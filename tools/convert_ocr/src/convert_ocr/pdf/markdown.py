"""Texto -> Markdown (port de legacy/extract_markdown.lines_to_markdown e md_tables.page_markdown_with_tables).

  * Titulos (#, ##, ###) por tamanho de fonte relativo ao corpo e por secoes numeradas em negrito.
  * Negrito / italico preservados (**texto**, _texto_).
  * Paragrafos refluidos (linhas de um mesmo bloco unidas; hifenizacao desfeita), preservando quebras em listas.
  * Marcadores de lista normalizados para "- ".
  * Tabelas GFM intercaladas na posicao do texto que elas continham.
"""

from __future__ import annotations

import pymupdf

from convert_ocr.pdf.textpage import (
    RE_BULLET, RE_BULLET_MD, RE_LIST_START, RE_NONALNUM, RE_NUM_ONLY, RE_SECTION, RE_WS, Line, Span,
)

MIN_HEADING_SIZE = 9.0   # tamanho minimo de fonte para titulo por tamanho (evita CAD/tabelas em 4pt)


def spans_to_md(spans: list[Span]) -> str:
    """Concatena spans aplicando **negrito** / _italico_, fundindo spans vizinhos de mesmo estilo."""
    merged: list[tuple[bool, bool, str]] = []
    for s in spans:
        key = (s.bold, s.italic)
        if merged and merged[-1][:2] == key:
            merged[-1] = (key[0], key[1], merged[-1][2] + s.text)
        else:
            merged.append((key[0], key[1], s.text))
    out = []
    for bold, italic, text in merged:
        core = text.strip()
        if not core or not (bold or italic) or not RE_NONALNUM.sub("", core):
            out.append(text)
            continue
        lead = text[: len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()):]
        mark = ("**" if bold else "") + ("_" if italic else "")
        close = ("_" if italic else "") + ("**" if bold else "")
        out.append(f"{lead}{mark}{core}{close}{trail}")
    s = "".join(out)
    s = s.replace("** **", " ").replace("_ _", " ")
    return RE_WS.sub(" ", s).strip()


def heading_level(ln: Line, body: float, max_chars: int, caps_block: bool = False,
                  short_block: bool = True) -> int:
    """0 = nao e titulo; 1..4 = nivel."""
    text = ln.text.strip()
    if not text or len(text) > max_chars or not RE_NONALNUM.sub("", text):
        return 0
    if text[-1] in ",;:":
        return 0
    ratio = ln.size / body if body else 1.0
    if ln.size >= MIN_HEADING_SIZE and body and short_block and text[-1] not in ".;":
        if ratio >= 1.5:
            return 1
        if ratio >= 1.25:
            return 2
        if ratio >= 1.12:
            return 3
    if ln.all_bold:
        m = RE_SECTION.match(text)
        if m:
            depth = m.group(1).count(".") + 1
            return min(depth + 1, 4)
        if caps_block and ratio >= 1.0 and text.isupper() and len(text) >= 4:
            return 3
    return 0


def block_info(lines: list[Line], max_chars: int) -> tuple[set[int], set[int]]:
    """(blocos curtos, blocos curtos em negrito e CAIXA ALTA). Curto = <= 3 linhas e <= max_chars."""
    by_block: dict[int, list[Line]] = {}
    for ln in lines:
        by_block.setdefault(ln.block, []).append(ln)
    short, caps = set(), set()
    for b, ls in by_block.items():
        txt = " ".join(l.text.strip() for l in ls)
        if len(ls) <= 3 and len(txt) <= max_chars:
            short.add(b)
            if len(txt) <= 100 and all(l.all_bold for l in ls) and txt.isupper():
                caps.add(b)
    return short, caps


FUSE_GAP_EM = 3.0        # fragmentos na mesma faixa de y a menos de 3 em de distancia sao a mesma linha
FUSE_Y_TOL = 0.3         # fracao da altura: desvio maximo entre os centros verticais


def fuse_row_fragments(lines: list[Line]) -> list[Line]:
    """Junta fragmentos de uma mesma linha visual (spec 04).

    Em texto justificado o MuPDF corta uma linha em varias quando o espaco entre palavras passa do seu limiar:
    "do", "servico", "publico" viram linhas separadas (mesmo bloco, mesma faixa de y, uma a direita da outra) e o
    markdown sairia com uma palavra por linha. Fragmentos consecutivos do mesmo bloco, horizontais, com centros
    verticais a menos de FUSE_Y_TOL da altura e separados por menos de FUSE_GAP_EM vezes o tamanho da fonte sao
    fundidos num unico Line (spans concatenados, com um espaco se faltar). Colunas lado a lado (gap grande) e
    linhas de blocos diferentes nao sao tocadas; as tabelas ja foram tratadas antes desta etapa."""
    if len(lines) < 2:
        return lines
    out: list[Line] = []
    for ln in lines:
        prev = out[-1] if out else None
        if (prev is not None and not ln.vertical and not prev.vertical and ln.block == prev.block
                and abs(ln.cy - prev.cy) <= FUSE_Y_TOL * max(ln.y1 - ln.y0, prev.y1 - prev.y0, 1.0)
                and ln.x0 >= prev.x1 - 1.0
                and ln.x0 - prev.x1 <= FUSE_GAP_EM * max(ln.size, prev.size, 1.0)):
            spans = list(prev.spans)
            if spans and not spans[-1].text.endswith((" ", "	")) and not ln.spans[0].text.startswith(" "):
                last = spans[-1]
                spans.append(Span(" ", last.size, last.bold, last.italic))
            spans.extend(ln.spans)
            out[-1] = Line(spans, prev.x0, min(prev.y0, ln.y0), max(prev.x1, ln.x1), max(prev.y1, ln.y1),
                           prev.block, False, prev.dy)
            continue
        out.append(ln)
    return out


CONT_GAP_EM = 0.8        # distancia vertical maxima (em alturas de fonte) entre uma linha e a sua continuacao
CONT_MIN_WIDTH = 0.85    # a linha anterior precisa ocupar >= isto da largura maxima da pagina (linha "cheia")


def merge_continuation_blocks(lines: list[Line]) -> list[Line]:
    """Junta no mesmo bloco linhas consecutivas que o MuPDF separou em blocos de UMA linha (alguns geradores
    emitem cada linha do paragrafo como bloco proprio): sem isto cada linha viraria um paragrafo.

    A linha seguinte continua a anterior quando as duas sao horizontais e do mesmo tamanho de fonte, a anterior
    e "cheia" (>= CONT_MIN_WIDTH da linha mais larga da pagina), a distancia vertical e a de um entrelinha
    (<= CONT_GAP_EM x tamanho da fonte) e a seguinte comeca na mesma margem ou a esquerda da anterior (um
    paragrafo novo comeca com recuo, a DIREITA). Blocos com mais de uma linha nunca sao tocados."""
    if len(lines) < 2:
        return lines
    count: dict[int, int] = {}
    for ln in lines:
        count[ln.block] = count.get(ln.block, 0) + 1
    max_w = max((ln.x1 - ln.x0 for ln in lines if not ln.vertical), default=0.0)
    if max_w <= 0:
        return lines
    out: list[Line] = []
    prev: Line | None = None
    prev_orig = -1                                  # bloco ORIGINAL da linha anterior (count nunca muda)
    for ln in lines:
        orig = ln.block
        if (prev is not None and count[orig] == 1 and count[prev_orig] == 1 and orig != prev_orig
                and not ln.vertical and not prev.vertical and abs(ln.size - prev.size) <= 0.5
                and (prev.x1 - prev.x0) >= CONT_MIN_WIDTH * max_w
                and 0.0 <= ln.y0 - prev.y1 <= CONT_GAP_EM * max(ln.size, 1.0)
                and ln.x0 <= prev.x0 + 1.0):
            ln = Line(ln.spans, ln.x0, ln.y0, ln.x1, ln.y1, prev.block, ln.vertical, ln.dy)
        out.append(ln)
        prev, prev_orig = ln, orig
    return out


def lines_to_markdown(lines: list[Line], body: float, heading_max_chars: int) -> tuple[str, int]:
    """Converte as linhas (ja sem cabecalho/rodape) de uma pagina em markdown. Retorna (md, n_titulos)."""
    lines = merge_continuation_blocks(fuse_row_fragments(lines))
    out: list[str] = []
    n_head = 0
    i = 0
    n = len(lines)
    short, caps = block_info(lines, heading_max_chars)

    def hl(l: Line) -> int:
        return heading_level(l, body, heading_max_chars, l.block in caps, l.block in short)

    while i < n:
        ln = lines[i]
        lvl = hl(ln)
        if lvl:
            parts = [ln]
            j = i + 1
            while j < n and lines[j].block == ln.block and hl(lines[j]) == lvl \
                    and not RE_SECTION.match(lines[j].text.strip()):
                parts.append(lines[j])
                j += 1
            text = " ".join(RE_WS.sub(" ", p.text).strip() for p in parts)
            text = text.replace("**", "")
            out.append("#" * lvl + " " + text)
            n_head += 1
            i = j
            continue

        j = i + 1
        while j < n and lines[j].block == ln.block and not hl(lines[j]):
            j += 1
        block = lines[i:j]
        max_w = max(l.x1 - l.x0 for l in block) or 1.0
        para: list[str] = []
        buf = ""
        prev: Line | None = None
        prev_num_only = False
        for l in block:
            md = spans_to_md(l.spans)
            if not md:
                continue
            raw = l.text.strip()
            is_list = bool(RE_LIST_START.match(raw))
            if RE_BULLET.match(raw):
                md = RE_BULLET_MD.sub(lambda m: "- " + m.group(1), md, count=1)
            if buf and prev is not None:
                prev_full = (prev.x1 - prev.x0) >= 0.85 * max_w
                gap = l.y0 - prev.y1
                same_para = prev_full and not is_list and gap < max(2.0, 0.8 * prev.size)
                if prev_num_only:
                    buf += " " + md
                elif same_para:
                    if buf.endswith("-") and md[:1].islower():
                        buf = buf[:-1] + md
                    else:
                        buf += " " + md
                else:
                    para.append(buf)
                    buf = md
            else:
                buf = md
            prev = l
            prev_num_only = bool(RE_NUM_ONLY.match(raw))
        if buf:
            para.append(buf)
        if para:
            out.append("\n".join(para).replace("** **", " ").replace("_ _", " "))
        i = j
    return "\n\n".join(out), n_head


def _in_cells(cx: float, cy: float, cells) -> bool:
    for x0, y0, x1, y1 in cells:
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return True
    return False


def page_markdown_with_tables(lines: list[Line], tables: list[tuple],
                              body: float, heading_max_chars: int) -> tuple[str, int]:
    """Markdown da pagina intercalando tabelas na posicao do texto que elas continham. Cada tabela e
    (bbox, md, celulas|None): uma linha de texto sai do fluxo se cai no bbox e, havendo celulas, dentro de uma
    delas (texto no bbox mas fora das celulas, como subtotais entre blocos, continua no fluxo)."""
    if not tables:
        return lines_to_markdown(lines, body, heading_max_chars)
    kept: list[Line] = []
    insert_at: dict[int, list[str]] = {}
    first_inside: list[int | None] = [None] * len(tables)
    for ln in lines:
        cx, cy = (ln.x0 + ln.x1) / 2, (ln.y0 + ln.y1) / 2
        inside = None
        for ti, tab in enumerate(tables):
            rect = tab[0]
            cells = tab[2] if len(tab) > 2 else None
            if rect.x0 <= cx <= rect.x1 and rect.y0 <= cy <= rect.y1 and (cells is None or _in_cells(cx, cy, cells)):
                inside = ti
                break
        if inside is None:
            kept.append(ln)
        elif first_inside[inside] is None:
            first_inside[inside] = len(kept)
    for ti, tab in enumerate(tables):
        rect, md = tab[0], tab[1]
        pos = first_inside[ti]
        if pos is None:  # tabela sem texto proprio: insere antes da 1a linha abaixo dela
            pos = next((i for i, ln in enumerate(kept) if ln.y0 >= rect.y1), len(kept))
        insert_at.setdefault(pos, []).append(md)
    parts: list[str] = []
    n_head = 0
    start = 0
    for pos in sorted(insert_at):
        if pos > start:
            md, nh = lines_to_markdown(kept[start:pos], body, heading_max_chars)
            n_head += nh
            if md:
                parts.append(md)
        parts.extend(insert_at[pos])
        start = pos
    if start < len(kept):
        md, nh = lines_to_markdown(kept[start:], body, heading_max_chars)
        n_head += nh
        if md:
            parts.append(md)
    return "\n\n".join(parts), n_head


def page_marker(pno: int, total: int) -> str:
    return f"> __Página {pno + 1} de {total}__"
