"""Motor `auto`: deteccao de HTML no markdown do pymupdf4llm e a troca pela montagem do rapidocr, sem OCR real
(os dois sub-motores sao dublados)."""

from __future__ import annotations

import numpy as np
import pytest

from convert_ocr.config import ENGINES, Settings, load_settings
from convert_ocr.core.engines.auto import AutoEngine
from convert_ocr.core.engines.base import PageOcr, Recognized, has_html


@pytest.mark.parametrize("md", [
    "| a | b |\n|---|---|\n| x<br>y | z |",
    "texto <span style=\"color:red\">vermelho</span>",
    "<table><tr><td>1</td></tr></table>",
    "nota<sup>1</sup>",
    "<img src=\"x.png\"/>",
    "antes <!-- comentario --> depois",
    "</div>",
])
def test_has_html_detecta_tags(md):
    assert has_html(md)


@pytest.mark.parametrize("md", [
    "", None, "# Titulo\n\nparagrafo normal",
    "valores < 10 e > 5",                 # comparacoes nao sao tags
    "a < b > c",
    "x <10> y",                           # numero nao e nome de tag
    "e-mail: <joao@exemplo.com>",         # autolink do markdown: '@' logo apos o nome nao e tag
    "| a | b |\n|---|---|\n| 1 | 2 |",
])
def test_has_html_ignora_texto_comum(md):
    assert not has_html(md)


def test_auto_e_o_default_e_esta_em_engines():
    assert ENGINES[0] == "auto" and Settings().engine == "auto"
    assert load_settings({"storage_dir": "/s"}).engine == "auto"
    with pytest.raises(ValueError, match="--storage"):                 # unico parametro obrigatorio
        load_settings({"engine": "auto"})


class _Rec:
    """Reconhecimento dublado: uma caixa, pagina 100x200 pt."""
    def __call__(self, page, settings):
        return Recognized(boxes=[], img=np.zeros((20, 10, 3), dtype=np.uint8), rotation=90, scale=1.0,
                          w=100.0, h=200.0, ms_render=3.0, ms_ocr=7.0)


class _P4:
    name = "pymupdf4llm"

    def __init__(self, md):
        self.md = md

    def from_recognized(self, pno, rec):
        return PageOcr(pno, rec.w, rec.h, md=self.md, rotation=rec.rotation, n_boxes=1, score=0.9,
                       ms_render=rec.ms_render, ms_ocr=rec.ms_ocr + 50.0, engine=self.name)


class _Rapid:
    name = "rapidocr"

    def from_recognized(self, pno, rec):
        return PageOcr(pno, rec.w, rec.h, line_rows=[[]], body=10.0, rotation=rec.rotation, n_boxes=1, score=0.9,
                       ms_render=rec.ms_render, ms_ocr=rec.ms_ocr, engine=self.name)


class _Page:
    number = 4


def _auto(monkeypatch, md: str) -> AutoEngine:
    eng = AutoEngine.__new__(AutoEngine)           # sem __init__: nao carrega modelos
    eng.settings = Settings(engine="auto")
    eng.p4llm, eng.rapid = _P4(md), _Rapid()
    monkeypatch.setattr("convert_ocr.core.engines.auto.recognize_page", _Rec())
    return eng


def test_auto_fica_com_o_pymupdf4llm_sem_html(monkeypatch):
    po = _auto(monkeypatch, "# Titulo\n\ntexto").ocr_page(_Page())
    assert po.engine == "pymupdf4llm" and po.md == "# Titulo\n\ntexto" and po.fallback == ""
    assert po.pno == 4 and po.rotation == 90 and po.ms_ocr == 57.0


def test_auto_troca_pelo_rapidocr_quando_ha_html(monkeypatch):
    po = _auto(monkeypatch, "| a |\n|---|\n| x<br>y |").ocr_page(_Page())
    assert po.engine == "rapidocr" and po.fallback == "html" and po.md is None and po.line_rows == [[]]
    assert po.rotation == 90 and po.ms_ocr == 57.0                      # tempo do layout contado uma vez so


def test_auto_forca_backend_rapidocr(monkeypatch):
    """O __init__ troca ocr_backend=tesseract por rapidocr antes de criar os sub-motores."""
    created = {}

    class FakeRapid:
        name = "rapidocr"
        def __init__(self, s): created["rapid"] = s

    class FakeP4:
        name = "pymupdf4llm"
        def __init__(self, s): created["p4"] = s

    monkeypatch.setattr("convert_ocr.core.engines.auto.RapidEngine", FakeRapid)
    monkeypatch.setattr("convert_ocr.core.engines.auto.Pymupdf4llmEngine", FakeP4)
    eng = AutoEngine(Settings(engine="auto", ocr_backend="tesseract"))
    assert created["p4"].ocr_backend == "rapidocr" and created["rapid"] is created["p4"]
    assert eng.backend == "rapidocr" and eng.settings.ocr_backend == "rapidocr"
