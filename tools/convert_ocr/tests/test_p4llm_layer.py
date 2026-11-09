"""Camada de texto do motor pymupdf4llm: tamanhos de fonte INTEIROS. O extrator de features do modelo de layout
(`pymupdf.features`, nativo) guarda em cache sem limite dados por tamanho de fonte; com um tamanho fracionario por
caixa o processo crescia ~4 MB por pagina de OCR (OOM em 2026-10-24)."""

from __future__ import annotations

import pymupdf

from convert_ocr.core.boxes import OcrBox
from convert_ocr.core.engines.p4llm import font_size, text_layer_doc


def test_font_size_is_whole_points_at_least_one():
    assert font_size(9.37) == 9.0
    assert font_size(23.999) == 23.0
    assert font_size(12.0) == 12.0
    assert font_size(0.3) == 1.0
    assert all(float(font_size(v)).is_integer() for v in (1.5, 7.25, 10.999, 40.01))


def test_text_layer_doc_writes_every_box_with_integer_font_size():
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 600), False)
    pix.clear_with(255)
    pix.set_dpi(72, 72)
    boxes = [OcrBox(20, 20 + i * 23.7, 20 + 150 + i * 7.3, 32.3 + i * 23.7, f"Linha {i} de teste", 0.9) for i in range(8)]
    boxes.append(OcrBox(370, 100, 384, 300, "texto vertical", 0.9, vertical=True))
    boxes.append(OcrBox(20, 500, 60, 510, "", 0.9))                      # caixa vazia: ignorada
    doc = text_layer_doc(pix, boxes, 400, 600)
    try:
        page = doc[0]
        spans = [s for b in page.get_text("dict")["blocks"] if b["type"] == 0 for ln in b["lines"] for s in ln["spans"]]
        assert spans
        sizes = [s["size"] for s in spans]
        assert all(abs(sz - round(sz)) < 1e-2 and sz >= 1 for sz in sizes), sizes
        assert len({round(sz) for sz in sizes}) >= 2                      # tamanhos variam com a caixa, mas inteiros
        text = page.get_text("text")
        assert all(f"Linha {i} de teste" in text for i in range(8))
        assert "texto vertical" in text
        assert page.get_image_info()                                      # imagem de fundo presente
    finally:
        doc.close()
