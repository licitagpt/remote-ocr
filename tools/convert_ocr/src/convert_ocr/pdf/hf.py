"""Cabecalho/rodape por COMPARACAO ENTRE PAGINAS (port de legacy/md_hf.py).

Resolve os casos que a comparacao por zona fixa nao resolvia:
  1. Carimbos VERTICAIS na margem lateral (1Doc, SUITE/CE, E-DOCS/ES): zonas "left"/"right".
  2. Timbre/cabecalho DENTRO DE UMA TABELA (moldura em volta do timbre): celulas passam por cell_filter().
  3. Variantes de texto entre paginas (hash por pagina, quebras, erros de OCR): comparacao fuzzy (difflib).

Algoritmo:
  * Candidatas: linhas nas zonas de margem - topo, base, ou verticais nas laterais.
  * Chave: casefold, digitos -> '#', sem espacos/pontuacao, sequencias hexadecimais -> '#'.
  * Agrupamento: chave exata na mesma zona, ou similaridade difflib >= `sim` na mesma zona e faixa vizinha.
  * Aceito: grupo presente em >= min_ratio das paginas (min. 3), ~1 ocorrencia por pagina, com letras alem de
    digitos e posicao estavel. Evita remover conteudo de tabela que apenas se repete ("Insumo", "UN").
  * Timbres altos: zonas estendidas em `ext`, aceitas apenas se encadeadas a um grupo aceito da zona principal.
  * Remocao: a 1a pagina em que o grupo aparece e mantida; numeracao de pagina e sempre removida.
  * strip_markdown(): para markdown ja pronto (layout), casa linhas inteiras ou por cobertura de grupos.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from math import ceil

from convert_ocr.pdf.textpage import RE_PAGENUM, Line, alnum_count, norm_text

CONTENT_MIN_ALNUM = 20   # linha "de conteudo" (nao numeracao/data) para a regra de contiguidade com a borda

_RE_HEX = re.compile(r"[#a-f]{8,}")     # hash hexadecimal (digitos ja viraram '#')
_RE_PUNCT = re.compile(r"[^\w#]+")      # espacos e pontuacao
FUZZY_MIN_CHARS = 12                    # chaves menores so casam por igualdade


def hf_key(text: str) -> str:
    """Chave de comparacao: casefold, digitos -> '#', sem espacos/pontuacao/sublinhados, hex -> '#'."""
    k = _RE_PUNCT.sub("", norm_text(text)).replace("_", "")
    return _RE_HEX.sub("#", k)


def _similar(a: str, b: str, thr: float) -> bool:
    la, lb = len(a), len(b)
    if abs(la - lb) > 0.3 * max(la, lb):
        return False
    sm = SequenceMatcher(None, a, b)
    return sm.real_quick_ratio() >= thr and sm.quick_ratio() >= thr and sm.ratio() >= thr


@dataclass(frozen=True)
class HFParams:
    top: float = 0.22          # zona de cabecalho (fracao da altura)
    bottom: float = 0.18       # zona de rodape
    side: float = 0.12         # zona lateral para linhas verticais (fracao da largura)
    ext: float = 0.13          # extensao das zonas topo/base para timbres altos (so encadeada)
    chain_bands: int = 2       # distancia maxima (faixas) para encadear a um grupo aceito
    min_ratio: float = 0.30    # presente em >= esta fracao das paginas do documento...
    min_pages: int = 3
    local_min_pages: int = 5   # ...OU em >= local_min_pages paginas e >= min_ratio das paginas entre a 1a e a ultima
                               # ocorrencia (documento-colecao: edital + anexos + apolice, cada um com o seu rodape)
    pos_tol: float = 0.03      # faixa de posicao (fracao da dimensao da pagina)
    stable_ratio: float = 0.8  # fracao das ocorrencias que deve cair na faixa modal +-1
    max_per_page: float = 1.5  # ocorrencias/pagina acima disto = conteudo repetido, nao cabecalho
    min_letters: int = 3       # chars alem de '#' que a chave precisa ter
    sim: float = 0.85          # similaridade difflib minima
    max_fuzzy_candidates: int = 60
    max_fit_pages: int = 1000  # documentos maiores: aprende nas primeiras N paginas e aplica a todas (= MAX_PAGES;
                               # 935 paginas custam ~3,5 s; o limite antigo de 400 deixava os anexos finais sem HF)


@dataclass
class _Cluster:
    zone: str
    key: str
    first_page: int
    pages: set[int] = field(default_factory=set)
    bands: Counter = field(default_factory=Counter)
    core: bool = False
    accepted: bool = False


class HFModel:
    def __init__(self, params: HFParams | None = None):
        self.p = params or HFParams()
        self.clusters: list[_Cluster] = []
        self._exact: dict[tuple[str, str], _Cluster] = {}
        self._by_band: dict[tuple[str, int], list[_Cluster]] = {}
        self.n_pages = 0
        self.removed_lines = 0
        self.removed_cells = 0

    # ------------------------------------------------------------------ zonas
    def zone(self, ln: Line, w: float, h: float) -> tuple[str, float, bool] | None:
        """(zona, posicao relativa a borda, na zona principal?) da linha, ou None se esta no corpo."""
        p = self.p
        if ln.vertical:
            cx = (ln.x0 + ln.x1) / 2
            if cx <= w * p.side:
                return "left", cx / w, True
            if cx >= w * (1 - p.side):
                return "right", (w - cx) / w, True
            return None
        if ln.y1 <= h * (p.top + p.ext):
            return "top", ln.y0 / h, ln.y1 <= h * p.top
        if ln.y0 >= h * (1 - p.bottom - p.ext):
            return "bottom", (h - ln.y1) / h, ln.y0 >= h * (1 - p.bottom)
        return None

    # ------------------------------------------------------------------ agrupamento
    def _fuzzy_find(self, zone: str, key: str, band: int, accepted_only: bool) -> _Cluster | None:
        if len(key) < FUZZY_MIN_CHARS:
            return None
        n = 0
        for b in (band, band - 1, band + 1):
            for c in self._by_band.get((zone, b), ()):
                if accepted_only and not c.accepted:
                    continue
                n += 1
                if n > self.p.max_fuzzy_candidates:
                    return None
                if _similar(key, c.key, self.p.sim):
                    return c
        return None

    def _add(self, zone: str, key: str, band: int, pno: int, core: bool = True) -> _Cluster:
        c = self._exact.get((zone, key))
        if c is None:
            c = self._fuzzy_find(zone, key, band, accepted_only=False)
            if c is not None:
                self._exact[(zone, key)] = c
        if c is None:
            c = _Cluster(zone, key, pno)
            self.clusters.append(c)
            self._exact[(zone, key)] = c
            self._by_band.setdefault((zone, band), []).append(c)
        c.pages.add(pno)
        c.bands[band] += 1
        c.core = c.core or core
        return c

    def _accept(self, clusters: list[_Cluster], n_pages: int) -> None:
        need = max(self.p.min_pages, ceil(n_pages * self.p.min_ratio))
        ok: list[_Cluster] = []
        for c in clusters:
            if len(c.pages) < need:
                # regra local: documento-colecao (edital + anexos + apolice...), cada parte com o seu cabecalho;
                # o grupo e denso no trecho em que aparece mesmo sendo raro no documento inteiro
                span = max(c.pages) - min(c.pages) + 1 if c.pages else 0
                if len(c.pages) < self.p.local_min_pages or len(c.pages) < ceil(span * self.p.min_ratio):
                    continue
            total = sum(c.bands.values())
            if total > self.p.max_per_page * len(c.pages):
                continue
            mode, _ = c.bands.most_common(1)[0]
            stable = sum(v for b, v in c.bands.items() if abs(b - mode) <= 1)
            if stable >= self.p.stable_ratio * total:
                ok.append(c)
        for c in ok:
            c.accepted = c.core
        pending = [c for c in ok if not c.accepted]
        changed = True
        while pending and changed:
            changed = False
            acc_bands = {(c.zone, c.bands.most_common(1)[0][0]) for c in ok if c.accepted}
            for c in pending:
                mode = c.bands.most_common(1)[0][0]
                if any((c.zone, mode + d) in acc_bands for d in range(-self.p.chain_bands, self.p.chain_bands + 1)):
                    c.accepted = True
                    changed = True
            pending = [c for c in pending if not c.accepted]

    def fit(self, pages: list[list[Line]], sizes: list[tuple[float, float]]) -> "HFModel":
        """Aprende os grupos a partir das linhas de todas as paginas (ou das primeiras max_fit_pages)."""
        self.n_pages = len(pages)
        if self.n_pages < self.p.min_pages:
            return self
        n_fit = min(self.n_pages, self.p.max_fit_pages)
        start = len(self.clusters)
        for pno in range(n_fit):
            lines, (w, h) = pages[pno], sizes[pno]
            for ln in lines:
                z = self.zone(ln, w, h)
                if z is None:
                    continue
                k = norm_text(ln.text)
                if RE_PAGENUM.match(k):
                    continue
                key = hf_key(ln.text)
                if len(key.replace("#", "")) < self.p.min_letters:
                    continue
                self._add(z[0], key, int(z[1] / self.p.pos_tol), pno, z[2])
        self._accept(self.clusters[start:], n_fit)
        return self

    @property
    def accepted(self) -> list[_Cluster]:
        return [c for c in self.clusters if c.accepted]

    # ------------------------------------------------------------------ consulta
    def match_text(self, text: str) -> _Cluster | None:
        """Grupo aceito que casa com o texto (qualquer zona; exato ou fuzzy)."""
        key = hf_key(text)
        if len(key) < 2:
            return None
        for zone in ("top", "bottom", "left", "right"):
            c = self._exact.get((zone, key))
            if c is not None and c.accepted:
                return c
        if len(key) < FUZZY_MIN_CHARS:
            return None
        for c in self.clusters:
            if c.accepted and _similar(key, c.key, self.p.sim):
                self._exact[(c.zone, key)] = c
                return c
        return None

    @staticmethod
    def is_pagenum(text: str) -> bool:
        return bool(RE_PAGENUM.match(norm_text(text)))

    # ------------------------------------------------------------------ remocao
    @staticmethod
    def _alone_in_row(ln: Line, lines: list[Line] | None) -> bool:
        """Nenhuma outra linha horizontal na mesma faixa de y (numero isolado = numeracao, nao celula)."""
        if lines is None:
            return True
        cy = (ln.y0 + ln.y1) / 2
        tol = max(2.0, 0.6 * (ln.y1 - ln.y0))
        for o in lines:
            if o is ln or o.vertical:
                continue
            if abs((o.y0 + o.y1) / 2 - cy) <= tol:
                return False
        return True

    @staticmethod
    def _numeric_short(text: str) -> bool:
        """Numero/data/hora curtos ("27/95", "31/12/2024 09:26"): o que varia de pagina em pagina num rodape."""
        t = text.strip()
        if not t or len(t) > 40:
            return False
        alnum = sum(ch.isalnum() for ch in t)
        digits = sum(ch.isdigit() for ch in t)
        return alnum > 0 and digits >= 0.4 * alnum

    def _substantial(self, text: str) -> bool:
        """Linha de conteudo (nao numeracao nem numero/data curtos) com texto suficiente."""
        return alnum_count(text) >= CONTENT_MIN_ALNUM and not self.is_pagenum(text) and not self._numeric_short(text)

    def _group_match(self, ln: Line, zone: str, pno: int) -> bool:
        """A linha casa com um grupo aceito (cabecalho/rodape repetido) visto em pagina anterior?"""
        key = hf_key(ln.text)
        c = self._exact.get((zone, key))
        if c is None and pno >= self.p.max_fit_pages:
            # paginas alem do fit: casa por texto (exato ou fuzzy) com os grupos aceitos
            c = self.match_text(ln.text)
            if c is not None and c.zone != zone:
                c = None
        return c is not None and c.accepted and c.first_page < pno

    @staticmethod
    def _beyond(ln: Line, o: Line, zone: str) -> bool:
        """`o` esta inteira alem de `ln` no sentido da borda da zona (abaixo no rodape, acima no cabecalho)."""
        tol = 0.3 * (ln.y1 - ln.y0)
        return o.y0 >= ln.y1 - tol if zone == "bottom" else o.y1 <= ln.y0 + tol

    def _stable_repeat(self, o: Line, zone: str) -> bool:
        """O texto se repete em varias paginas sempre na mesma posicao (grupo aceito ou nao: o nome que aparece
        duas vezes no quadro de assinaturas e rejeitado por max_per_page, mas e rodape do mesmo jeito)."""
        c = self._exact.get((zone, hf_key(o.text)))
        if c is None or len(c.pages) < self.p.min_pages:
            return False
        total = sum(c.bands.values())
        mode, _ = c.bands.most_common(1)[0]
        stable = sum(v for b, v in c.bands.items() if abs(b - mode) <= 1)
        return stable >= self.p.stable_ratio * total

    def _content_beyond(self, ln: Line, zone: str, w: float, h: float, pno: int, lines: list[Line]) -> bool:
        """Entre a linha e a borda da pagina ha conteudo de verdade? Entao a linha e corpo que se repete
        (rotulo de formulario, "Tratamento Diferenciado: Nao" no fim de cada item), nao rodape/cabecalho.
        Texto repetido em posicao estavel (rodape nao aceito, nomes do quadro de assinaturas) nao conta."""
        for o in lines:
            if o is ln or o.vertical or not self._beyond(ln, o, zone):
                continue
            if not self._substantial(o.text):
                continue
            oz = self.zone(o, w, h)
            if oz is not None and oz[0] == zone and (self._group_match(o, zone, pno) or self._stable_repeat(o, zone)):
                continue
            return True
        return False

    def _edge_row_with_hf(self, ln: Line, zone: str, w: float, h: float, pno: int, lines: list[Line]) -> bool:
        """A linha esta na faixa extrema da pagina (nada alem dela) e divide a faixa com um rodape/cabecalho
        aceito: e o numero de pagina / data desse rodape ("Edital 20/2024 ........ 27/95")."""
        cy = (ln.y0 + ln.y1) / 2
        tol = max(2.0, 0.6 * (ln.y1 - ln.y0))
        mate = False
        for o in lines:
            if o is ln or o.vertical:
                continue
            if self._beyond(ln, o, zone):
                return False
            if abs((o.y0 + o.y1) / 2 - cy) <= tol:
                oz = self.zone(o, w, h)
                if oz is not None and oz[0] == zone and self._group_match(o, zone, pno):
                    mate = True
        return mate

    def is_hf(self, ln: Line, w: float, h: float, pno: int, lines: list[Line] | None = None) -> bool:
        """A linha e cabecalho/rodape a remover nesta pagina? `lines` (todas da pagina) permite distinguir
        um numero de pagina isolado de uma celula numerica de tabela que caiu na zona de margem."""
        z = self.zone(ln, w, h)
        if z is None:
            return False
        if self.is_pagenum(ln.text):
            text = ln.text.strip()
            if any(ch.isalpha() for ch in text):          # "Pagina 3 de 10", "fls. 12": sempre
                return True
            # so digitos ("3", "3/10", "- 3 -"): precisa estar na zona principal e sozinho na linha
            if z[2] and self._alone_in_row(ln, lines):
                return True
            if len(text) <= 1:
                return False
        if self._group_match(ln, z[0], pno):
            # texto repetido que casa com um grupo aceito, mas com conteudo de verdade entre ele e a borda da
            # pagina (horizontal apenas): rotulo repetido do corpo, nao cabecalho/rodape
            if lines is not None and not ln.vertical and z[0] in ("top", "bottom") \
                    and self._content_beyond(ln, z[0], w, h, pno, lines):
                return False
            return True
        # numero de pagina / data / hora na mesma faixa de um rodape ou cabecalho aceito, na borda da pagina
        if lines is not None and not ln.vertical and z[2] and z[0] in ("top", "bottom") \
                and self._numeric_short(ln.text) and self._edge_row_with_hf(ln, z[0], w, h, pno, lines):
            return True
        return False

    def filter_lines(self, lines: list[Line], w: float, h: float, pno: int) -> list[Line]:
        """Linhas da pagina sem cabecalho/rodape (1a pagina de cada grupo mantida)."""
        kept: list[Line] = []
        for ln in lines:
            if self.is_hf(ln, w, h, pno, lines):
                self.removed_lines += 1
            else:
                kept.append(ln)
        return kept

    def cell_filter(self, lines: list[Line], w: float, h: float, pno: int):
        """(bbox da celula, texto) -> texto sem as linhas de cabecalho/rodape que caem na celula."""
        def f(rect, text: str) -> str:
            x0, y0, x1, y1 = rect
            inside = [ln for ln in lines
                      if x0 <= (ln.x0 + ln.x1) / 2 <= x1 and y0 <= (ln.y0 + ln.y1) / 2 <= y1]
            if not inside:
                return text
            kept = [ln for ln in inside if not self.is_hf(ln, w, h, pno, lines)]
            self.removed_cells += len(inside) - len(kept)
            if len(kept) == len(inside):
                return text
            kept.sort(key=lambda l: (round(l.y0), l.x0))
            return "\n".join(ln.text.strip() for ln in kept)
        return f

    # ------------------------------------------------------------------ markdown pronto (layout)
    _RE_MD_MARK = re.compile(r"<[^>]+>|[*_#>`\\]+")

    def _plain(self, line: str) -> str:
        return self._RE_MD_MARK.sub(" ", line).strip()

    def _is_hf_text(self, text: str, pno: int, digits_ok: bool = True) -> bool:
        """`digits_ok=False`: um numero sozinho nao conta como numeracao (celulas de tabela)."""
        if self.is_pagenum(text) and (digits_ok or any(ch.isalpha() for ch in text)):
            return True
        c = self.match_text(text)
        return c is not None and c.first_page < pno

    def coverage(self, text: str, pno: int) -> float:
        """Fracao da chave do texto coberta por chaves de grupos aceitos (vistos em pagina anterior)."""
        key = hf_key(text)
        if len(key) < 4:
            return 0.0
        keys = sorted({c.key for c in self.clusters if c.accepted and c.first_page < pno
                       and len(c.key.replace("#", "")) >= 3}, key=len, reverse=True)
        if not keys:
            return 0.0
        i, covered, n = 0, 0, len(key)
        while i < n:
            for k in keys:
                if key.startswith(k, i):
                    covered += len(k)
                    i += len(k)
                    break
            else:
                i += 1
        return covered / n

    @staticmethod
    def _is_table_sep(st: str) -> bool:
        return st.startswith("|") and st.endswith("|") and set(st) <= set("|-: ") and "-" in st

    def _filter_table_row(self, st: str, pno: int, min_coverage: float) -> str | None:
        cells = st[1:-1].split("|")
        if sum(1 for c in cells if self._plain(c)) <= 2:
            new = []
            for c in cells:
                parts = []
                for part in c.split("<br>"):
                    t = self._plain(part)
                    if t and self._is_hf_text(t, pno, digits_ok=False):
                        self.removed_cells += 1
                        continue
                    parts.append(part)
                new.append("<br>".join(parts))
            cells = new
        plain_row = " ".join(self._plain(c) for c in cells if self._plain(c))
        if not plain_row:
            return None
        if self.coverage(plain_row, pno) >= min_coverage:
            self.removed_cells += 1
            return None
        return "|" + "|".join(cells) + "|"

    def strip_markdown(self, md: str, pno: int, min_coverage: float = 0.8) -> str:
        """Remove de um markdown pronto as linhas que casam com grupo aceito visto em pagina anterior."""
        lines = md.split("\n")
        out: list[str] = []
        i = 0
        while i < len(lines):
            st = lines[i].strip()
            if not st:
                out.append(lines[i])
                i += 1
                continue
            if st.startswith("|") and st.endswith("|"):
                rows: list[str] = []
                sep = None
                while i < len(lines) and lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
                    s = lines[i].strip()
                    if self._is_table_sep(s):
                        sep = sep or s
                    else:
                        r = self._filter_table_row(s, pno, min_coverage)
                        if r is not None:
                            rows.append(r)
                    i += 1
                if rows:
                    ncols = rows[0].count("|") - 1
                    out.append(rows[0])
                    out.append(sep if sep and sep.count("|") - 1 == ncols else "|" + "---|" * ncols)
                    out.extend(rows[1:])
                continue
            t = self._plain(st)
            if t and (self._is_hf_text(t, pno) or self.coverage(t, pno) >= min_coverage):
                self.removed_lines += 1
                i += 1
                continue
            out.append(lines[i])
            i += 1
        return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
