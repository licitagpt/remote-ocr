"""Extracao estruturada de texto por pagina (port de legacy/extract_markdown.py).

`extract_lines(page)` devolve as linhas de texto da pagina com posicao, estilo e orientacao, ja no espaco
exibido (paginas com /Rotate sao corrigidas), mais estatisticas de caracteres problematicos (U+FFFD, area de
uso privado) usadas pela classificacao.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

import pymupdf

FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_LIGATURES | pymupdf.TEXT_MEDIABOX_CLIP
REPLACEMENT = "�"
OCR_TAG, DRAW_TAG = "OCR", "DRAW"   # marcadores [OCR:n] [DRAW:n]: a pagina sai SO com o marcador; n conta por documento
BLANK_MARK = "[BLANK]"              # pagina em branco: sai com a paginacao normal e so este marcador (sem contador)


def mark(tag: str, n: int) -> str:
    return f"[{tag}:{n}]"

RE_WS = re.compile(r"\s+")
RE_ZERO_WIDTH = re.compile("[" + REPLACEMENT + "​‌‍⁠﻿­]")
RE_NONALNUM = re.compile(r"[\W_]+")
RE_TOKEN = re.compile(r"\S+")
RE_DIGITS = re.compile(r"\d+")
# numeracao de pagina isolada, aplicada sobre norm_text (digitos ja virados '#')
RE_PAGENUM = re.compile(
    r"^[(\[]?(?:(?:p[áa]g(?:ina)?|page|folha|fls?|fl)\.?\s*(?:n[ºo°]?\.?)?\s*:?\s*)?#(?:\s*(?:de|of|/|-)\s*#)?[)\]]?$"
    r"|^[-–—]\s*#\s*[-–—]$",
    re.IGNORECASE,
)
RE_BULLET = re.compile(r"^\s*[•▪■●○◦·\-–—*]\s+")
RE_BULLET_MD = re.compile(r"^((?:\*\*|_)*)\s*[•▪■●○◦·\-–—*]\s+")   # marcador possivelmente dentro de **/_
RE_NUM_ONLY = re.compile(r"^\d+(?:\.\d+)*\.?$")                      # linha que e so o numero do item
RE_LIST_START = re.compile(
    r"^\s*(?:[•▪■●○◦·\-–—*]\s+|[a-z]\)|[a-z]\.\s|\(?[ivxIVX]+[\.\)]\s|\d+(?:\.\d+)*[\.\)]?(?:\s|$)|[A-Z]\)\s)"
)
RE_SECTION = re.compile(r"^(\d+(?:\.\d+)*)\.?\s*[-–—:]?\s+\S")
RE_BOLD_FONT = re.compile(r"bold|black|heavy|semibold|demibold", re.IGNORECASE)
RE_ITALIC_FONT = re.compile(r"italic|oblique", re.IGNORECASE)
RE_NUM = re.compile(r"^\(?[-+]?\d[\d.,]*%?\)?$")
# numeracao de item ("6.1", "6.1.", "1)", "(a)", "iv.") ou marcador; numero sozinho conta no inicio da celula
RE_ITEMNUM = re.compile(
    r"^(?:\(?\d+(?:\.\d+)+\.?|\(?\d+[.)]|\(?[a-z][.)]|[ivx]+[.)]|[•▪■●○◦·\-–—*\ue000-\uf8ff-])(?:\s|$)|^\d+$",
    re.IGNORECASE)   # marcadores incluem glifos de uso privado (Wingdings/Symbol: bullets, setas, checkboxes)
# area de uso privado (fontes de simbolos / icones): contada por regex, nao por laco Python char a char
RE_PUA = re.compile("[\ue000-\uf8ff\U000f0000-\U0010fffd]")

# (x0, y0, x1, y1, texto, bloco, linha, n) como devolvido por TextPage.extractWORDS(), coordenadas NAO rotacionadas
Word = tuple[float, float, float, float, str, int, int, int]


@dataclass(slots=True)
class Span:
    text: str
    size: float
    bold: bool
    italic: bool


@dataclass(slots=True)
class Line:
    spans: list[Span]
    x0: float
    y0: float
    x1: float
    y1: float
    block: int
    vertical: bool = False     # texto girado 90 graus (carimbos laterais)
    dy: float = 0.0            # componente vertical da direcao de escrita (sinal: para baixo > 0)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    @property
    def size(self) -> float:
        return max(s.size for s in self.spans)

    @property
    def all_bold(self) -> bool:
        return all(s.bold for s in self.spans if s.text.strip())

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass(slots=True)
class TextStats:
    chars: int = 0          # chars (apos limpeza) em todas as linhas
    fffd: int = 0           # U+FFFD removidos (glifos sem unicode)
    pua: int = 0            # chars em area de uso privado (fontes de simbolos / icones)


_font_style_cache: dict[str, tuple[bool, bool]] = {}


def font_style(font: str) -> tuple[bool, bool]:
    """(bold, italic) pelo nome da fonte; cache por nome (poucas fontes, milhares de spans)."""
    st = _font_style_cache.get(font)
    if st is None:
        st = (bool(RE_BOLD_FONT.search(font)), bool(RE_ITALIC_FONT.search(font)))
        if len(_font_style_cache) < 4096:
            _font_style_cache[font] = st
    return st


def make_textpage(page: pymupdf.Page) -> pymupdf.TextPage:
    """TextPage da pagina com as flags do conversor: criado UMA vez e reaproveitado por linhas e palavras."""
    return page.get_textpage(flags=FLAGS)


def extract_words(textpage: pymupdf.TextPage) -> list[Word]:
    """Palavras (coordenadas nao rotacionadas) do TextPage ja criado; ~0,5 ms por pagina, em C."""
    return textpage.extractWORDS()


def rotate_words(page: pymupdf.Page, words: list[Word]) -> list[Word]:
    """Palavras no espaco exibido: aplica page.rotation_matrix quando a pagina tem /Rotate (extractWORDS devolve
    coordenadas da pagina NAO rotacionada; o clip da grade, o find_tables e as linhas usam o espaco exibido)."""
    if not page.rotation:
        return words
    a, b, c, d, e, f = page.rotation_matrix
    out: list[Word] = []
    for w in words:
        x0, y0, x1, y1 = w[0], w[1], w[2], w[3]
        xa, ya = a * x0 + c * y0 + e, b * x0 + d * y0 + f
        xb, yb = a * x1 + c * y1 + e, b * x1 + d * y1 + f
        out.append((min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb), w[4], w[5], w[6], w[7]))
    return out


def page_words(page: pymupdf.Page) -> list[Word]:
    """Palavras da pagina no espaco exibido, de um TextPage novo (quando nao ha varredura para reaproveitar)."""
    return rotate_words(page, extract_words(make_textpage(page)))


def extract_lines(page: pymupdf.Page, textpage: pymupdf.TextPage | None = None) -> tuple[list[Line], TextStats]:
    """Linhas de texto da pagina no espaco exibido + estatisticas de caracteres."""
    d = page.get_text("dict", flags=FLAGS, textpage=textpage) if textpage is not None else page.get_text("dict", flags=FLAGS)
    # get_text devolve coordenadas da pagina NAO rotacionada; page.rect e a pagina como exibida.
    rot = page.rotation
    rmat = page.rotation_matrix if rot else None
    stats = TextStats()
    lines: list[Line] = []
    for bi, b in enumerate(d.get("blocks", ())):
        if b.get("type", 0) != 0:
            continue
        for ln in b.get("lines", ()):
            spans: list[Span] = []
            for s in ln.get("spans", ()):
                raw = s.get("text", "")
                if not raw:
                    continue
                stats.fffd += raw.count(REPLACEMENT)
                t = RE_ZERO_WIDTH.sub("", raw)
                if not t:
                    continue
                stats.pua += len(RE_PUA.findall(t))
                stats.chars += len(t)
                flags = s.get("flags", 0)
                fbold, fitalic = font_style(s.get("font", ""))
                spans.append(Span(
                    text=t,
                    size=float(s.get("size", 0.0)),
                    bold=bool(flags & 16) or fbold,
                    italic=bool(flags & 2) or fitalic,
                ))
            if spans and any(s.text.strip() for s in spans):
                x0, y0, x1, y1 = ln["bbox"]
                dx, dy = ln.get("dir", (1.0, 0.0))
                if rmat is not None:
                    r = pymupdf.Rect(x0, y0, x1, y1) * rmat
                    x0, y0, x1, y1 = r.x0, r.y0, r.x1, r.y1
                    if rot in (90, 270):
                        dx, dy = dy, dx
                lines.append(Line(spans, x0, y0, x1, y1, bi, vertical=abs(dy) > abs(dx), dy=float(dy)))
    return lines, stats


def sideways_rotation(lines: list[Line], min_chars: int = 200, min_ratio: float = 0.6) -> int:
    """Se a maior parte do texto da pagina esta girada 90 graus (tabela deitada numa pagina retrato, sem
    /Rotate), devolve a rotacao adicional (90 ou 270) que a deixa em pe; senao 0."""
    total = sum(len(ln.text) for ln in lines)
    if total < min_chars:
        return 0
    vert = [ln for ln in lines if ln.vertical]
    vchars = sum(len(ln.text) for ln in vert)
    if vchars / total < min_ratio:
        return 0
    down = sum(len(ln.text) for ln in vert if ln.dy > 0)
    # texto escrito de cima para baixo fica em pe girando a pagina 270 (= -90); de baixo para cima, 90
    return 270 if down >= vchars / 2 else 90


def norm_text(t: str) -> str:
    return RE_WS.sub(" ", RE_DIGITS.sub("#", t.casefold())).strip()


def is_pagenum(text: str) -> bool:
    return bool(RE_PAGENUM.match(norm_text(text)))


def alnum_count(text: str) -> int:
    return len(RE_NONALNUM.sub("", text))


def body_font_size(pages: list[list[Line]]) -> float:
    """Tamanho de fonte do corpo: moda (ponderada por chars) dos spans em linhas de prosa (>= 30 chars).
    Sem linhas de prosa (tabelas / pranchas CAD) retorna 0 -> nao ha titulos por tamanho."""
    hist: Counter[float] = Counter()
    for lines in pages:
        for ln in lines:
            if len(ln.text.strip()) < 30:
                continue
            for s in ln.spans:
                hist[round(s.size * 2) / 2] += len(s.text.strip())
    return hist.most_common(1)[0][0] if hist else 0.0


def horizontal(lines: list[Line]) -> list[Line]:
    return [ln for ln in lines if not ln.vertical]
