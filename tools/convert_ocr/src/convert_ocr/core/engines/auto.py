"""Motor `auto` (default): o RapidOCR reconhece a pagina UMA vez; o pymupdf4llm (modelo de layout) monta o markdown
e, se nessa pagina ele devolver tags HTML (celulas com <br>, <span>, tabelas em HTML...), vale a montagem propria
do motor rapidocr, feita das mesmas caixas reconhecidas. Os dois caminhos usam o reconhecedor RapidOCR: o
`ocr_backend` tesseract nao se aplica aqui."""

from __future__ import annotations

import logging
from dataclasses import replace

import pymupdf

from convert_ocr.config import Settings
from convert_ocr.core.engines.base import OcrEngine, PageOcr, has_html, recognize_page
from convert_ocr.core.engines.p4llm import Pymupdf4llmEngine
from convert_ocr.core.engines.rapid import RapidEngine

log = logging.getLogger("convert_ocr.engine.auto")


class AutoEngine(OcrEngine):
    name = "auto"
    backend = "rapidocr"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        if settings.ocr_backend != "rapidocr":
            log.info("motor auto usa sempre o reconhecedor rapidocr (ocr_backend=%s ignorado)", settings.ocr_backend)
            settings = replace(settings, ocr_backend="rapidocr")
        self.settings = settings
        self.rapid = RapidEngine(settings)
        self.p4llm = Pymupdf4llmEngine(settings)

    def info(self) -> dict:
        d = super().info()
        d["engines"] = [self.p4llm.name, self.rapid.name]
        return d

    def ocr_page(self, page: pymupdf.Page) -> PageOcr:
        rec = recognize_page(page, self.settings)
        po = self.p4llm.from_recognized(page.number, rec)
        if not has_html(po.md):
            return po
        log.debug("p%d: pymupdf4llm devolveu HTML; usando a montagem do rapidocr", page.number + 1)
        alt = self.rapid.from_recognized(page.number, rec)
        alt.ms_ocr = po.ms_ocr                 # o tempo do layout ja foi gasto: conta uma vez so
        alt.fallback = "html"
        return alt
