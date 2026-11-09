"""Motor `rapidocr`: PyMuPDF renderiza a pagina, o RapidOCR reconhece, e a montagem do markdown e nossa
(core/boxes.py + convert_pdf.core.markdown): linhas visuais -> blocos -> paragrafos, listas, titulos, tabelas."""

from __future__ import annotations

import logging

import pymupdf

from convert_ocr.config import Settings
from convert_ocr.core.boxes import rows_to_lines, to_rows
from convert_ocr.core.engines.base import OcrEngine, PageOcr, RapidRecognizer, Recognized, page_score, recognize_page

log = logging.getLogger("convert_ocr.engine.rapidocr")


class RapidEngine(OcrEngine):
    name = "rapidocr"
    backend = "rapidocr"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        RapidRecognizer.get(settings)        # modelos e dispositivo prontos antes da 1a pagina (warm-up do server)

    def ocr_page(self, page: pymupdf.Page) -> PageOcr:
        return self.from_recognized(page.number, recognize_page(page, self.settings))

    def from_recognized(self, pno: int, rec: Recognized) -> PageOcr:
        """Monta a pagina a partir de um reconhecimento ja feito (o motor auto reconhece uma vez para os dois)."""
        rows = to_rows(rec.boxes)
        lines, line_rows, body = rows_to_lines(rows, [b for b in rec.boxes if b.vertical])
        return PageOcr(pno, rec.w, rec.h, lines=lines, line_rows=line_rows, body=body, rotation=rec.rotation,
                       n_boxes=len(rec.boxes), score=page_score(rec.boxes), ms_render=rec.ms_render,
                       ms_ocr=rec.ms_ocr, engine=self.name)
