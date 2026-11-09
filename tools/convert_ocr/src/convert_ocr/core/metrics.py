"""Resultado do OCR de um documento: contadores, tempos por etapa e status. Serializavel (JSON/CSV)."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Timings:
    open: float = 0.0
    extract: float = 0.0     # linhas nativas das paginas de texto (para o cabecalho/rodape)
    render: float = 0.0      # renderizacao das paginas de OCR
    ocr: float = 0.0         # reconhecimento (+ layout no motor pymupdf4llm)
    hf: float = 0.0
    convert: float = 0.0     # montagem do markdown
    write: float = 0.0
    tokens: float = 0.0
    total: float = 0.0

    def as_dict(self) -> dict[str, int]:
        return {k: int(round(v)) for k, v in asdict(self).items()}


class Timer:
    """`with Timer() as t: ...; t.ms`"""
    def __enter__(self):
        self._t = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.ms = (time.perf_counter() - self._t) * 1000


@dataclass
class OcrResult:
    pdf: str
    md: str = ""                     # .md de entrada (o original do convert_pdf)
    out: str = ""                    # .md gravado (igual a `md` quando substitui o original)
    status: str = ""                 # ok | no_ocr | skipped | error
    error_code: str = ""
    error_message: str = ""
    engine: str = ""
    backend: str = ""                # reconhecedor usado (rapidocr | tesseract)
    pages: int = 0                   # paginas do PDF
    ocr_pages: int = 0               # paginas [OCR:n] encontradas no .md
    ocr_done: int = 0                # paginas de OCR tratadas
    empty_pages: int = 0             # paginas de OCR sem texto reconhecido (ficam so com a paginacao)
    rotated_pages: int = 0           # paginas giradas 90/270 pelo auto-rotate
    low_score_pages: int = 0         # paginas com confianca media < 0.7
    html_pages: int = 0              # motor auto: paginas em que o pymupdf4llm devolveu HTML e valeu a montagem do rapidocr
    lines: int = 0                   # linhas reconhecidas (apos cabecalho/rodape)
    hf_groups: int = 0
    hf_removed: int = 0
    tables: int = 0
    headings: int = 0
    chars: int = 0                   # chars de markdown produzidos pelo OCR
    size_bytes: int = 0              # tamanho do PDF
    md_size_bytes: int = 0           # tamanho do .md gravado (UTF-8)
    token_count: int = 0             # tokens do .md final (tiktoken)
    warnings: int = 0
    ms: Timings = field(default_factory=Timings)

    @property
    def ok(self) -> bool:
        return self.status != "error"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ms"] = self.ms.as_dict()
        d["ok"] = self.ok
        d["tamanhoMd"] = self.md_size_bytes          # nome consumido pelo Workers (.NET)
        d.pop("error_code", None)
        d.pop("error_message", None)
        if not self.ok:
            d["error"] = {"code": self.error_code, "message": self.error_message}
        return d

    def to_flat(self) -> dict[str, Any]:
        d = asdict(self)
        ms = d.pop("ms")
        d.update({f"ms_{k}": int(round(v)) for k, v in ms.items()})
        return d
