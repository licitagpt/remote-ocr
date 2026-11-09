"""Leitura e reescrita do .md gerado pelo convert_pdf.

Formato (convert_pdf/core/document.py, etapa 5): blocos `> __Página N de M__` seguidos do corpo da pagina,
separados por linhas em branco; paginas de OCR tem como corpo so o marcador `[OCR:n]` (n conta por documento a
partir de 1); no fim pode haver `> __Documento truncado: convertidas N de M páginas__`.

`parse()` localiza as paginas e os marcadores de OCR; `rebuild()` remonta o documento no mesmo formato,
trocando o corpo das paginas indicadas. Paginas nao tocadas saem byte a byte como entraram (o formato e o
mesmo que o convert_pdf grava).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

RE_MARK = re.compile(r"^> __Página (\d+) de (\d+)__$", re.M)
RE_TRUNC = re.compile(r"^> __Documento truncado: convertidas (\d+) de (\d+) páginas__$", re.M)
RE_OCR = re.compile(r"^\[OCR:(\d+)\]$")
RE_ANY_MARK = re.compile(r"^> __(?:Página \d+ de \d+|Documento truncado: .*)__\s*$", re.M)


@dataclass(slots=True)
class MdPage:
    page: int                 # 1-based
    body: str                 # corpo sem espacos nas pontas
    ocr_n: int | None = None  # n de [OCR:n] quando a pagina e de OCR


@dataclass(slots=True)
class MdDoc:
    prefix: str               # texto antes do 1o marcador (normalmente vazio)
    total: int                # M dos marcadores
    pages: list[MdPage] = field(default_factory=list)
    truncated: tuple[int, int] | None = None   # (convertidas, total) se houver marcador de truncamento

    @property
    def ocr_pages(self) -> list[MdPage]:
        return [p for p in self.pages if p.ocr_n is not None]

    def page_marker(self, page: int) -> str:
        return f"> __Página {page} de {self.total}__"


def page_marker(pno: int, total: int) -> str:
    return f"> __Página {pno + 1} de {total}__"


def parse(text: str) -> MdDoc:
    """Separa o .md em paginas. Levanta ValueError se nao houver marcadores de pagina."""
    text = text.replace("\r\n", "\n")
    marks = list(RE_MARK.finditer(text))
    if not marks:
        raise ValueError("o .md nao tem marcadores de pagina ('> __Página N de M__'); nao e saida do convert_pdf")
    trunc = RE_TRUNC.search(text)
    doc = MdDoc(prefix=text[:marks[0].start()], total=int(marks[0].group(2)))
    if trunc:
        doc.truncated = (int(trunc.group(1)), int(trunc.group(2)))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end]
        if trunc and trunc.start() >= m.end() and trunc.start() < end:
            body = body[:trunc.start() - m.end()]
        body = body.strip()
        ocr = RE_OCR.match(body)
        doc.pages.append(MdPage(int(m.group(1)), body, int(ocr.group(1)) if ocr else None))
    return doc


def rebuild(doc: MdDoc, new_bodies: dict[int, str] | None = None) -> str:
    """Remonta o .md no formato do convert_pdf, com os corpos de `new_bodies` ({pagina 1-based: markdown})
    no lugar dos originais. Corpo vazio -> a pagina sai so com o marcador de paginacao."""
    new_bodies = new_bodies or {}
    parts: list[str] = []
    for p in doc.pages:
        body = new_bodies.get(p.page, p.body).strip()
        marker = doc.page_marker(p.page)
        parts.append(f"{marker}\n\n{body}" if body else marker)
    if doc.truncated:
        parts.append(f"> __Documento truncado: convertidas {doc.truncated[0]} de {doc.truncated[1]} páginas__")
    return doc.prefix + "\n\n".join(parts) + "\n"


def sanitize_body(md: str) -> str:
    """Markdown de uma pagina vindo do OCR: sem linhas que o leitor confundiria com marcadores de pagina, sem
    marcadores [OCR:n]/[DRAW:n]/[BLANK] literais isolados e sem mais de uma linha em branco seguida."""
    md = md.replace("\r\n", "\n").replace("\x00", "")
    md = RE_ANY_MARK.sub("", md)
    md = re.sub(r"^\[(?:OCR|DRAW):\d+\]\s*$|^\[BLANK\]\s*$", "", md, flags=re.M)
    md = re.sub(r"[ \t]+$", "", md, flags=re.M)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()
