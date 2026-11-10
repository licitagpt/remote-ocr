"""Caixas de texto do OCR -> linhas (`convert_pdf.core.textpage.Line`) -> markdown.

O RapidOCR devolve, por pagina, uma lista de quadrilateros (4 pontos, em pixels da imagem) com o texto e a
confianca. Aqui:

  1. `to_boxes()` converte para `OcrBox` em pontos da pagina, descarta confianca baixa e marca texto vertical;
  2. `to_rows()` agrupa as caixas horizontais em LINHAS VISUAIS (mesma faixa de y) e, dentro da linha, em
     CELULAS (fragmentos separados por um vao maior que ~2,5 caracteres: colunas de tabela, "rotulo .... valor");
  3. `rows_to_lines()` transforma celulas em `Line` com bloco (paragrafo) atribuido pelo espacamento vertical,
     tamanho de fonte quantizado (OCR da alturas ruidosas: so >= 1,3x o corpo conta como maior) e negrito
     sintetico em titulos curtos em CAIXA ALTA / secoes numeradas, que e o que `lines_to_markdown` usa para
     decidir titulos;
  4. `detect_tables()` acha sequencias de >= 3 linhas visuais com >= 2 celulas alinhadas e as emite como tabela
     GFM, intercalada no texto por `page_markdown_with_tables`.

Tudo o que e montagem de markdown (paragrafos, listas, titulos) e do convert_pdf: a pagina de OCR sai com o
mesmo estilo das paginas de texto do mesmo documento.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from statistics import median

import pymupdf

from convert_ocr.pdf.markdown import page_markdown_with_tables
from convert_ocr.pdf.textpage import RE_SECTION, Line, Span, alnum_count

CELL_GAP_CHARS = 2.5       # vao entre fragmentos (em larguras de caractere) a partir do qual sao celulas distintas
ROW_TOL = 0.5              # fracao da altura: desvio maximo do centro vertical para estar na mesma linha visual
BLOCK_GAP = 0.8            # vao vertical (em alturas de linha) a partir do qual comeca outro bloco/paragrafo
SIZE_FACTOR = 0.8          # altura da caixa -> tamanho de fonte aproximado (pt)
BIG_RATIO = 1.3            # altura >= BIG_RATIO x corpo conta como fonte maior (titulo h2); >= HUGE_RATIO, h1
HUGE_RATIO = 1.6
MIN_TABLE_ROWS = 3
TABLE_ROW_GAP = 1.5         # vao vertical (em alturas de linha) que encerra uma corrida de linhas de tabela
TABLE_CONT_MAX_WIDTH = 0.6 # linha de 1 celula dentro da tabela e continuacao se ocupar menos que isto da largura da tabela
CAPS_MAX_CHARS = 100       # titulo em caixa alta: bloco curto ate isto
SECTION_MAX_CHARS = 80     # "5. Levantamento de Mercado": uma linha ate isto vira titulo numerado


@dataclass(slots=True)
class OcrBox:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    score: float
    vertical: bool = False
    fh: float = 0.0           # altura de UMA caixa reconhecida (~altura da fonte); a bbox de uma celula fundida e maior

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def font_h(self) -> float:
        return self.fh or self.h

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def char_w(self) -> float:
        n = max(1, len(self.text.strip()))
        return (self.w / n) if not self.vertical else (self.h / n)


def to_boxes(quads, texts, scores, scale: float, min_score: float) -> list[OcrBox]:
    """Quadrilateros do RapidOCR (pixels) -> OcrBox em pontos (divide por `scale` = pixels por ponto)."""
    out: list[OcrBox] = []
    if quads is None or texts is None:
        return out
    for q, t, s in zip(quads, texts, scores if scores is not None else [1.0] * len(texts)):
        text = re.sub(r"\s+", " ", str(t)).strip()
        if not text or float(s) < min_score:
            continue
        xs = [float(p[0]) for p in q]
        ys = [float(p[1]) for p in q]
        # orientacao: a 1a aresta (p0 -> p1) segue a direcao de leitura
        dx, dy = float(q[1][0]) - float(q[0][0]), float(q[1][1]) - float(q[0][1])
        vertical = abs(dy) > abs(dx) * 1.5 and len(text) > 1
        out.append(OcrBox(min(xs) / scale, min(ys) / scale, max(xs) / scale, max(ys) / scale, text, float(s), vertical))
    return out


def vertical_share(boxes: list[OcrBox]) -> float:
    """Fracao dos caracteres reconhecidos que estao em caixas verticais (pagina deitada?)."""
    total = sum(len(b.text) for b in boxes)
    if not total:
        return 0.0
    return sum(len(b.text) for b in boxes if b.vertical) / total


def quality(boxes: list[OcrBox]) -> float:
    """Soma de confianca x caracteres: compara leituras da mesma pagina em orientacoes diferentes."""
    return sum(b.score * len(b.text) for b in boxes)


def mean_score(boxes: list[OcrBox]) -> float:
    n = sum(len(b.text) for b in boxes)
    return sum(b.score * len(b.text) for b in boxes) / n if n else 0.0


# ---------------------------------------------------------------------- linhas visuais e celulas
def to_rows(boxes: list[OcrBox]) -> list[list[OcrBox]]:
    """Agrupa caixas horizontais em linhas visuais (lista de listas de celulas, de cima para baixo e da esquerda
    para a direita). Fragmentos proximos na mesma linha sao fundidos numa celula so."""
    hor = sorted((b for b in boxes if not b.vertical), key=lambda b: (b.cy, b.x0))
    rows: list[list[OcrBox]] = []
    cur: list[OcrBox] = []
    cur_cy = cur_h = 0.0
    for b in hor:
        if cur and abs(b.cy - cur_cy) <= ROW_TOL * min(b.h, cur_h):
            cur.append(b)
            n = len(cur)
            cur_cy = (cur_cy * (n - 1) + b.cy) / n
            cur_h = (cur_h * (n - 1) + b.h) / n
        else:
            if cur:
                rows.append(_merge_cells(cur))
            cur, cur_cy, cur_h = [b], b.cy, b.h
    if cur:
        rows.append(_merge_cells(cur))
    return rows


def _merge_cells(frags: list[OcrBox]) -> list[OcrBox]:
    frags = sorted(frags, key=lambda b: b.x0)
    cells: list[OcrBox] = []
    for b in frags:
        if cells:
            prev = cells[-1]
            gap = b.x0 - prev.x1
            thr = CELL_GAP_CHARS * min(prev.char_w, b.char_w)
            if gap <= max(thr, 0.3 * min(prev.h, b.h)):
                cells[-1] = OcrBox(prev.x0, min(prev.y0, b.y0), max(prev.x1, b.x1), max(prev.y1, b.y1),
                                   f"{prev.text} {b.text}", min(prev.score, b.score), fh=max(prev.font_h, b.font_h))
                continue
        cells.append(b)
    return cells


# ---------------------------------------------------------------------- Line
def _body_height(rows: list[list[OcrBox]]) -> float:
    hs = [c.font_h for r in rows for c in r if len(c.text) >= 30]
    if not hs:
        hs = [c.font_h for r in rows for c in r]
    return median(hs) if hs else 10.0


def rows_to_lines(rows: list[list[OcrBox]], vertical: list[OcrBox]) -> tuple[list[Line], list[list[Line]], float]:
    """(linhas, linhas visuais como listas de Line, tamanho do corpo). Blocos: vao vertical entre linhas visuais
    > BLOCK_GAP x altura -> bloco novo. Tamanho: quantizado em corpo / h2 / h1."""
    body_h = _body_height(rows)
    body = max(9.0, SIZE_FACTOR * body_h)

    def size_of(h: float) -> float:
        r = h / body_h if body_h else 1.0
        if r >= HUGE_RATIO:
            return body * HUGE_RATIO
        if r >= BIG_RATIO:
            return body * BIG_RATIO
        return body

    lines: list[Line] = []
    line_rows: list[list[Line]] = []
    page_x0 = min((c.x0 for r in rows for c in r), default=0.0)
    page_x1 = max((c.x1 for r in rows for c in r), default=0.0)
    page_w = page_x1 - page_x0
    max_w = max((max(c.x1 for c in r) - min(c.x0 for c in r) for r in rows), default=0.0)
    block = 0
    title_blocks: set[int] = set()
    prev_y1: float | None = None
    prev_h = body_h
    prev_cells = 1
    prev_title = False
    for ri, r in enumerate(rows):
        y0 = min(c.y0 for c in r)
        y1 = max(c.y1 for c in r)
        h = max(c.h for c in r)
        title = False
        # vizinhas com varias celulas = regiao de tabela/formulario: caixa alta ali e celula, nao titulo
        in_grid = (ri > 0 and len(rows[ri - 1]) > 1) or (ri + 1 < len(rows) and len(rows[ri + 1]) > 1)
        if len(r) == 1 and not in_grid:
            c = r[0]
            centered = page_w > 0 and abs((c.x0 + c.x1) / 2 - (page_x0 + page_x1) / 2) <= 0.10 * page_w
            title = _title_like(c.text, (c.w / max_w) if max_w else 1.0, centered)
        if prev_y1 is not None:
            gap = y0 - prev_y1
            if gap > BLOCK_GAP * max(h, prev_h) or (len(r) > 1) != (prev_cells > 1) or title != prev_title:
                block += 1
        if title:
            title_blocks.add(block)
        row_lines = [Line([Span(c.text, size_of(c.font_h), False, False)], c.x0, c.y0, c.x1, c.y1, block) for c in r]
        lines.extend(row_lines)
        line_rows.append(row_lines)
        prev_y1, prev_h, prev_cells, prev_title = y1, h, len(r), title
    for v in vertical:
        block += 1
        lines.append(Line([Span(v.text, body, False, False)], v.x0, v.y0, v.x1, v.y1, block, vertical=True, dy=1.0))
    _synthetic_bold(line_rows, title_blocks)
    return lines, line_rows, body


def _title_like(text: str, width_ratio: float, centered: bool) -> bool:
    """Linha curta e nao cheia que e titulo: secao numerada ("5. Levantamento de Mercado") ou CAIXA ALTA centrada
    ("TERMO DE DISPENSA DE LICITACAO 009/2023"). Vira bloco proprio para que `heading_level` a avalie sozinha
    (no mesmo bloco do paragrafo seguinte ela nunca seria titulo). Caixa alta a esquerda (nomes em formularios,
    celulas de tabela) nao conta."""
    t = text.strip()
    if not t or t[-1] in ".,;:" or width_ratio >= 0.85 or alnum_count(t) < 4:
        return False
    if centered and len(t) <= CAPS_MAX_CHARS and t.isupper() and sum(ch.isalpha() for ch in t) >= 4:
        return True
    return len(t) <= SECTION_MAX_CHARS and bool(RE_SECTION.match(t)) and alnum_count(t) >= 6


def _synthetic_bold(line_rows: list[list[Line]], title_blocks: set[int]) -> None:
    """Negrito sintetico nos blocos-titulo (ver `_title_like`): `heading_level` so promove a titulo linhas em
    negrito (secao numerada -> nivel pela profundidade; caixa alta curta -> nivel 3)."""
    for row in line_rows:
        for l in row:
            if l.block in title_blocks:
                for s in l.spans:
                    s.bold = True


# ---------------------------------------------------------------------- tabelas
def _escape_cell(t: str) -> str:
    return t.replace("|", "\\|").strip()


def detect_tables(line_rows: list[list[Line]]) -> list[tuple[pymupdf.Rect, str, None]]:
    """Corridas de linhas visuais vizinhas (sem vao de paragrafo) com >= MIN_TABLE_ROWS linhas de >= 2 celulas ->
    tabela GFM. Linhas de 1 celula ESTREITA dentro da corrida sao continuacao da celula de cima (texto que quebrou
    dentro da celula). Colunas: medianas de x0 das celulas nas linhas com o numero modal de celulas; cada celula
    vai para a coluna de x0 mais proximo (colisoes sao fundidas)."""
    tables: list[tuple[pymupdf.Rect, str, None]] = []
    n = len(line_rows)

    def geo(row: list[Line]) -> tuple[float, float, float, float]:
        return (min(l.x0 for l in row), min(l.y0 for l in row), max(l.x1 for l in row), max(l.y1 for l in row))

    i = 0
    while i < n:
        if len(line_rows[i]) < 2:
            i += 1
            continue
        run = [line_rows[i]]
        x0, y0, x1, y1 = geo(line_rows[i])
        j = i + 1
        while j < n:
            row = line_rows[j]
            rx0, ry0, rx1, ry1 = geo(row)
            h = max(ry1 - ry0, y1 - max(l.y0 for l in run[-1]))
            gap = ry0 - y1
            if gap > TABLE_ROW_GAP * h:
                break
            if len(row) < 2 and (gap > BLOCK_GAP * h or (rx1 - rx0) >= TABLE_CONT_MAX_WIDTH * (x1 - x0)):
                break                            # linha solta: paragrafo depois da tabela, nao continuacao de celula
            run.append(row)
            x0, y0, x1, y1 = min(x0, rx0), y0, max(x1, rx1), max(y1, ry1)
            j += 1
        i = j
        multi = [r for r in run if len(r) >= 2]
        if len(multi) < MIN_TABLE_ROWS:
            continue
        counts = sorted(len(r) for r in multi)
        ncols = max(set(counts), key=counts.count)
        if ncols < 2 or counts.count(ncols) < 0.6 * len(multi):
            continue
        anchors = [median(r[k].x0 for r in multi if len(r) == ncols) for k in range(ncols)]
        if any(anchors[k + 1] - anchors[k] < 2.0 for k in range(ncols - 1)):
            continue

        def col_of(l: Line) -> int:
            return min(range(ncols), key=lambda k: abs(anchors[k] - l.x0))

        md_rows: list[list[str]] = []
        for r in run:
            if len(r) < 2 and md_rows:                       # continuacao: junta na celula de cima
                k = col_of(r[0])
                md_rows[-1][k] = (md_rows[-1][k] + " " + _escape_cell(r[0].text)).strip()
                continue
            cells = [""] * ncols
            for c in r:
                k = col_of(c)
                cells[k] = (cells[k] + " " + _escape_cell(c.text)).strip()
            md_rows.append(cells)
        rx0, ry0, rx1, ry1 = geo([l for r in run for l in r])
        out = ["| " + " | ".join(md_rows[0]) + " |", "|" + "---|" * ncols]
        out += ["| " + " | ".join(r) + " |" for r in md_rows[1:]]
        tables.append((pymupdf.Rect(rx0, ry0, rx1, ry1), chr(10).join(out), None))
    return tables


# ---------------------------------------------------------------------- markdown da pagina
def page_markdown(lines: list[Line], line_rows: list[list[Line]], body: float, heading_max_chars: int,
                  with_tables: bool = True) -> tuple[str, int, int]:
    """(markdown, n_titulos, n_tabelas) das linhas (ja sem cabecalho/rodape). `line_rows` devem conter so as
    linhas presentes em `lines` (ver `filter_rows`)."""
    tables = detect_tables(line_rows) if with_tables else []
    md, nh = page_markdown_with_tables(lines, tables, body, heading_max_chars)
    return md, nh, len(tables)


def filter_rows(line_rows: list[list[Line]], kept: list[Line]) -> list[list[Line]]:
    ids = {id(l) for l in kept}
    out = []
    for row in line_rows:
        r = [l for l in row if id(l) in ids]
        if r:
            out.append(r)
    return out
