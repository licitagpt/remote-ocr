"""Motor `pymupdf4llm`: PyMuPDF renderiza a pagina; um reconhecedor (RapidOCR ou Tesseract) gera a CAMADA DE
TEXTO; o pymupdf4llm (com o modelo de layout `pymupdf.layout`, se instalado) monta o markdown a partir dela:
titulos, paragrafos, listas e tabelas vem do modelo de layout, nao da nossa montagem.

A camada de texto e posta numa pagina LIMPA (imagem renderizada + texto invisivel), num documento em memoria:
  * a pagina original fica intacta e sem os efeitos colaterais do `force_ocr` do pymupdf4llm (texto em fonte
    de fallback com matriz de largura e marcas ~~tachado~~ quando o PDF tem letras vetorizadas);
  * o modelo de layout ve exatamente a mesma imagem que o reconhecedor viu.

Backends:
  * rapidocr  - mesmo reconhecedor do motor rapidocr (idioma OCR_LANG); texto invisivel caixa a caixa;
  * tesseract - `Pixmap.pdfocr_tobytes` (Tesseract embutido no PyMuPDF; precisa dos dados *.traineddata em
                --tessdata ou TESSDATA_PREFIX do Tesseract).

Tamanho da fonte do texto invisivel: INTEIRO (pontos). O extrator de features do modelo de layout
(`pymupdf.features`, nativo, pymupdf-layout 2.0) guarda em cache, sem limite e fora do heap Python, dados por
tamanho de fonte; com um tamanho fracionario diferente por caixa (ajuste exato a largura) o processo crescia ~4 MB
por pagina de OCR ate o OOM (5 processos x 21 GB em 2026-10-24). Com tamanhos inteiros o conjunto e finito e o
RSS fica estavel (medido: +0,02 MB/pagina). O tamanho so influi nas features do layout (titulos por tamanho);
a largura da caixa continua respeitada porque o tamanho e arredondado, nao aumentado alem do que cabe.
"""

from __future__ import annotations

import logging

import pymupdf

from convert_ocr.config import Settings
from convert_ocr.core import device as dev
from convert_ocr.core.boxes import OcrBox
from convert_ocr.core.engines.base import (OcrEngine, PageOcr, RapidRecognizer, Recognized, page_score, pixmap_from,
                                           recognize_page)
from convert_ocr.core.metrics import Timer
from convert_pdf.core.textpage import extract_lines

log = logging.getLogger("convert_ocr.engine.pymupdf4llm")

FONTNAME = "ocrtext"
_font: pymupdf.Font | None = None
_layout_warned = False


def _ocr_font() -> pymupdf.Font:
    global _font
    if _font is None:
        _font = pymupdf.Font("cjk")      # Droid Sans Fallback: cobre latim acentuado e simbolos comuns
    return _font


def _p4llm():
    """Importa o pymupdf4llm uma vez, em modo layout se o pymupdf.layout estiver instalado (ver
    convert_pdf.pymupdf_compat: com PyMuPDF 2 o import e bloqueado ate device.activate_layout liberar)."""
    global _layout_warned
    from convert_pdf.pymupdf_compat import import_pymupdf4llm
    pymupdf4llm = import_pymupdf4llm()
    if not getattr(pymupdf4llm, "_use_layout", False) and not _layout_warned:
        _layout_warned = True
        log.warning("pymupdf.layout nao instalado: o motor pymupdf4llm roda sem o modelo de layout "
                    "(pip install -e 'tools/convert_ocr[layout]')")
    return pymupdf4llm


def font_size(fit: float) -> float:
    """Tamanho de fonte (pt) do texto invisivel de uma caixa: o maior INTEIRO que cabe (`fit` = tamanho exato que
    preencheria a caixa), minimo 1. Inteiro por causa do cache nativo do `pymupdf.features` (ver docstring do modulo)."""
    return max(1.0, float(int(fit)))


def text_layer_doc(pix: pymupdf.Pixmap, boxes: list[OcrBox], w: float, h: float) -> pymupdf.Document:
    """Documento de uma pagina (w x h pontos) com a imagem e o texto reconhecido invisivel sobre cada caixa."""
    doc = pymupdf.open()
    page = doc.new_page(width=w, height=h)
    page.insert_image(page.rect, pixmap=pix)
    if boxes:
        font = _ocr_font()
        page.insert_font(fontname=FONTNAME, fontbuffer=font.buffer)
        for b in boxes:
            text = b.text
            if not text:
                continue
            if b.vertical:
                # texto deitado: escreve-o girado na caixa (fonte pela largura da caixa)
                fs = font_size(min(b.w, b.h / max(font.text_length(text, 1), 1e-3)))
                page.insert_text((b.x0 + 0.8 * b.w, b.y1), text, fontsize=fs, fontname=FONTNAME, render_mode=3, rotate=90)
                continue
            fs = font_size(min(b.h * 0.9, b.w / max(font.text_length(text, 1), 1e-3)))
            page.insert_text((b.x0, b.y1 - 0.2 * b.h), text, fontsize=fs, fontname=FONTNAME, render_mode=3)
    return doc


class Pymupdf4llmEngine(OcrEngine):
    name = "pymupdf4llm"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.backend = settings.ocr_backend
        if self.backend == "tesseract" and not settings.effective_tessdata:
            raise RuntimeError("backend tesseract: dados do Tesseract nao encontrados (--tessdata ou TESSDATA_PREFIX do Tesseract); "
                               "use scripts/get_tessdata.py ou --ocr-backend rapidocr")
        self.device = dev.probe(settings)
        dev.activate_layout(self.device)     # antes de importar o pymupdf4llm, que ativaria o layout em CPU
        _p4llm()
        if self.backend == "rapidocr":
            RapidRecognizer.get(settings)

    def info(self) -> dict:
        d = super().info()
        d["tesseract"] = "cpu" if self.backend == "tesseract" else None
        return d

    # ------------------------------------------------------------------ camada de texto
    def _doc_tesseract(self, page: pymupdf.Page) -> tuple[pymupdf.Document, float, float, float]:
        s = self.settings
        with Timer() as tr:
            pix = page.get_pixmap(dpi=s.ocr_dpi, colorspace=pymupdf.csRGB, alpha=False)
        data = pix.pdfocr_tobytes(language=s.tess_lang, tessdata=s.effective_tessdata)
        doc = pymupdf.open("pdf", data)
        r = doc[0].rect
        return doc, r.width, r.height, tr.ms

    @staticmethod
    def _markdown(doc: pymupdf.Document) -> tuple[list, str]:
        """Linhas da camada de texto e o markdown do pymupdf4llm para a (unica) pagina do documento; fecha o doc."""
        try:
            lines, _ = extract_lines(doc[0])
            md = ""
            if lines:
                p4 = _p4llm()
                md = p4.to_markdown(doc, pages=[0], use_ocr=False, header=False, footer=False,
                                    show_progress=False, page_chunks=False)
                if isinstance(md, list):      # versoes antigas devolvem lista com page_chunks
                    md = "\n".join(c.get("text", "") for c in md)
            return lines, md
        finally:
            doc.close()

    # ------------------------------------------------------------------ pagina
    def ocr_page(self, page: pymupdf.Page) -> PageOcr:
        if self.backend != "tesseract":
            return self.from_recognized(page.number, recognize_page(page, self.settings))
        with Timer() as to:
            doc, w, h, ms_render = self._doc_tesseract(page)
            lines, md = self._markdown(doc)
        return PageOcr(page.number, w, h, lines=lines, md=md, n_boxes=len(lines), score=1.0 if lines else 0.0,
                       ms_render=ms_render, ms_ocr=max(0.0, to.ms - ms_render), engine=self.name)

    def from_recognized(self, pno: int, rec: Recognized) -> PageOcr:
        """Monta a pagina a partir de um reconhecimento RapidOCR ja feito: camada de texto numa pagina limpa e
        markdown do pymupdf4llm (o motor auto reconhece uma vez para os dois motores)."""
        with Timer() as to:
            doc = text_layer_doc(pixmap_from(rec.img, self.settings.ocr_dpi), rec.boxes, rec.w, rec.h)
            lines, md = self._markdown(doc)
        return PageOcr(pno, rec.w, rec.h, lines=lines, md=md, rotation=rec.rotation, n_boxes=len(rec.boxes),
                       score=page_score(rec.boxes), ms_render=rec.ms_render, ms_ocr=rec.ms_ocr + to.ms,
                       engine=self.name)
