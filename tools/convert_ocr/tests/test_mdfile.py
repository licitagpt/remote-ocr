"""Leitura/reescrita do .md do convert_pdf: paginas, marcadores [OCR:n], truncamento, remontagem byte a byte."""

from __future__ import annotations

import pytest

from convert_ocr.core import mdfile

MD = """> __Página 1 de 5__

# Titulo

texto da pagina 1

> __Página 2 de 5__

[OCR:1]

> __Página 3 de 5__

[BLANK]

> __Página 4 de 5__

[OCR:2]

> __Página 5 de 5__

[DRAW:1]
"""


def test_parse_pages_and_ocr():
    d = mdfile.parse(MD)
    assert d.total == 5 and [p.page for p in d.pages] == [1, 2, 3, 4, 5]
    assert d.pages[0].body.startswith("# Titulo") and d.pages[0].ocr_n is None
    assert [(p.page, p.ocr_n) for p in d.ocr_pages] == [(2, 1), (4, 2)]
    assert d.pages[2].body == "[BLANK]" and d.pages[4].body == "[DRAW:1]"
    assert d.truncated is None and d.prefix == ""


def test_rebuild_is_identity_without_changes():
    assert mdfile.rebuild(mdfile.parse(MD)) == MD


def test_rebuild_replaces_only_ocr_pages():
    d = mdfile.parse(MD)
    out = mdfile.rebuild(d, {2: "## Reconhecido\n\nlinha", 4: ""})
    assert "[OCR:" not in out
    assert "> __Página 2 de 5__\n\n## Reconhecido\n\nlinha\n\n> __Página 3 de 5__" in out
    assert "> __Página 4 de 5__\n\n> __Página 5 de 5__" in out        # corpo vazio: so a paginacao
    assert "[BLANK]" in out and "[DRAW:1]" in out and "# Titulo" in out
    again = mdfile.parse(out)
    assert again.ocr_pages == [] and again.pages[1].body == "## Reconhecido\n\nlinha"


def test_truncated_marker_preserved():
    md = "> __Página 1 de 3__\n\n[OCR:1]\n\n> __Página 2 de 3__\n\ntexto\n\n> __Documento truncado: convertidas 2 de 3 páginas__\n"
    d = mdfile.parse(md)
    assert d.truncated == (2, 3) and len(d.pages) == 2 and d.pages[1].body == "texto"
    out = mdfile.rebuild(d, {1: "ocr"})
    assert out.endswith("> __Documento truncado: convertidas 2 de 3 páginas__\n") and "[OCR:1]" not in out
    assert mdfile.rebuild(d) == md


def test_parse_rejects_non_convert_pdf_output():
    with pytest.raises(ValueError):
        mdfile.parse("# so um markdown qualquer\n")


def test_sanitize_body():
    md = "texto  \n\n\n\n> __Página 9 de 9__\n[OCR:3]\nfim\n"
    assert mdfile.sanitize_body(md) == "texto\n\nfim"
    assert mdfile.sanitize_body("   ") == ""
