"""O convert_ocr e autocontido: nao importa nem depende do pacote convert_pdf (os modulos compartilhados estao em
convert_ocr.pdf). Garante que uma correcao futura nao reintroduza a dependencia sem querer."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import convert_ocr  # noqa: F401  (instala o bloqueio do pymupdf.layout)
from convert_ocr.core import boxes, device, document, mdfile  # noqa: F401
from convert_ocr.core.engines import base, p4llm, rapid  # noqa: F401
from convert_ocr.runtime import server  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def test_no_convert_pdf_module_is_imported():
    assert not [m for m in sys.modules if m == "convert_pdf" or m.startswith("convert_pdf.")]


def test_source_has_no_convert_pdf_imports():
    offenders = []
    for py in (ROOT / "src").rglob("*.py"):
        for line in py.read_text(encoding="utf-8").splitlines():
            if re.match(r"^\s*(from|import)\s+convert_pdf\b", line):
                offenders.append(f"{py.relative_to(ROOT)}: {line.strip()}")
    assert offenders == []


def test_pyproject_has_no_convert_pdf_dependency():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    deps = text.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert "convert_pdf" not in re.sub(r"#.*", "", deps)
