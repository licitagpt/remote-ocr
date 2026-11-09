"""Pipeline por documento com o motor rapidocr de verdade (marcados `ocr`): substituicao do .md, cabecalho/rodape,
erros tipados e o original intacto em caso de falha."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from convert_ocr.core import mdfile
from convert_ocr.core.document import process

pytestmark = pytest.mark.ocr


def _markers(md: str):
    return re.findall(r"^> __Página (\d+) de (\d+)__$", md, flags=re.M)


def test_mixed_document_replaces_only_ocr_pages(mixed: Path, settings):
    md = mixed.with_suffix(".md")
    before = mdfile.parse(md.read_text(encoding="utf-8"))
    assert [p.page for p in before.ocr_pages] == [2, 4]
    r = process(mixed, None, None, settings)
    assert r.status == "ok", (r.error_code, r.error_message)
    assert r.ocr_pages == 2 and r.ocr_done == 2 and r.empty_pages == 0 and r.pages == 5
    text = md.read_text(encoding="utf-8")
    assert "[OCR:" not in text and _markers(text) == [(str(i), "5") for i in range(1, 6)]
    after = mdfile.parse(text)
    for pno in (0, 2, 4):                                        # paginas de texto byte a byte como estavam
        assert after.pages[pno].body == before.pages[pno].body
    p2, p4 = after.pages[1].body, after.pages[3].body
    assert "pregao eletronico" in p2.replace("\n", " ") and "2.1." in p2
    assert "PREFEITURA MUNICIPAL" not in p2 and "Rua das Flores" not in p2       # cabecalho/rodape herdado das paginas de texto
    assert "Pagina 2 de 5" not in p2
    assert "DO PAGAMENTO" in p4 and r.hf_removed >= 4
    assert r.md_size_bytes == len(text.encode("utf-8")) and r.chars > 200 and r.lines > 6


def test_out_dir_keeps_original(mixed: Path, settings, tmp_path):
    out = tmp_path / "saida" / "mixed.md"
    original = mixed.with_suffix(".md").read_text(encoding="utf-8")
    r = process(mixed, None, out, settings)
    assert r.status == "ok" and out.is_file() and "[OCR:" not in out.read_text(encoding="utf-8")
    assert mixed.with_suffix(".md").read_text(encoding="utf-8") == original


def test_output_ocr_md_keeps_original(mixed: Path, settings):
    from convert_ocr.core.document import ocr_md_path
    md = mixed.with_suffix(".md")
    original = md.read_text(encoding="utf-8")
    r = process(mixed, None, None, settings, output="ocr-md")
    side = ocr_md_path(md)
    assert side.name == "mixed.ocr.md" and side.parent == md.parent
    assert r.status == "ok" and Path(r.out) == side and side.is_file()
    assert "[OCR:" not in side.read_text(encoding="utf-8")
    assert md.read_text(encoding="utf-8") == original
    assert ocr_md_path(Path("a/b/x.MD")).name == "x.ocr.md" and ocr_md_path(Path("x")).name == "x.ocr.md"


def test_run_local_defaults_to_ocr_md(mixed: Path, settings):
    from convert_ocr.runtime.local import run_local
    md = mixed.with_suffix(".md")
    original = md.read_text(encoding="utf-8")
    assert run_local(settings, str(mixed), None, None, True, None) == 0
    assert md.with_name("mixed.ocr.md").is_file() and md.read_text(encoding="utf-8") == original
    assert settings.output_for("local") == "ocr-md" and settings.output_for("server") == "replace"
    assert replace(settings, output="replace").output_for("local") == "replace"
    assert run_local(replace(settings, output="replace"), str(mixed), None, None, True, None) == 0
    assert "[OCR:" not in md.read_text(encoding="utf-8")                       # agora substituiu


def test_all_scanned_document(storage: Path, settings):
    pdf = storage / "files" / "pncp" / "2025" / "12345678000199" / "000001" / "scan.pdf"
    r = process(pdf, None, None, settings)
    assert r.status == "ok" and r.ocr_pages == 3 == r.ocr_done
    text = pdf.with_suffix(".md").read_text(encoding="utf-8")
    assert "[OCR:" not in text and "DO OBJETO" in text and "DA JUSTIFICATIVA" in text
    # cabecalho repetido: 1a ocorrencia mantida, demais removidas
    assert text.count("PREFEITURA MUNICIPAL") <= 1


def test_blank_scan_gives_empty_page(storage: Path, settings):
    pdf = storage / "files" / "pncp" / "2025" / "12345678000199" / "000001" / "blank.pdf"
    r = process(pdf, None, None, settings)
    assert r.status == "ok" and r.ocr_done == 1 and r.empty_pages == 1
    text = pdf.with_suffix(".md").read_text(encoding="utf-8")
    assert text == "> __Página 1 de 1__\n"


def test_errors_are_typed_and_leave_md_untouched(storage: Path, mixed: Path, settings, tmp_path):
    root = mixed.parent
    original = mixed.with_suffix(".md").read_text(encoding="utf-8")
    assert process(root / "nao-existe.pdf", None, None, settings).error_code == "NOT_FOUND"
    assert process(root / "semmd.pdf", None, None, settings).error_code == "NO_MD"
    bad = tmp_path / "bad.md"
    bad.write_text("# sem marcadores\n", encoding="utf-8")
    assert process(mixed, bad, None, settings).error_code == "BAD_MD"
    other = tmp_path / "other.md"
    other.write_text(original.replace("de 5__", "de 6__"), encoding="utf-8")
    assert process(mixed, other, None, settings).error_code == "MD_MISMATCH"
    assert process(mixed, None, None, replace(settings, max_ocr_pages=1)).error_code == "TOO_MANY_PAGES"
    r = process(mixed, None, None, replace(settings, timeout_s=0.001))
    assert r.status == "error" and r.error_code == "TIMEOUT"
    assert mixed.with_suffix(".md").read_text(encoding="utf-8") == original
    r = process(mixed, None, None, settings, engine="inexistente")
    assert r.status == "error" and r.error_code == "INTERNAL"


def test_no_ocr_pages_is_noop(mixed: Path, settings):
    md = mixed.with_suffix(".md")
    text = md.read_text(encoding="utf-8").replace("[OCR:1]", "texto").replace("[OCR:2]", "texto")
    md.write_text(text, encoding="utf-8")
    r = process(mixed, None, None, settings)
    assert r.status == "no_ocr" and r.ok and r.ocr_pages == 0 and md.read_text(encoding="utf-8") == text


def test_result_serializable(mixed: Path, settings):
    import json
    r = process(mixed, None, None, settings)
    d = r.to_dict()
    json.dumps(d)
    assert d["ok"] is True and d["tamanhoMd"] == r.md_size_bytes and "error" not in d and d["engine"] == "rapidocr"
    assert set(d["ms"]) >= {"render", "ocr", "hf", "convert", "write", "total"}


@pytest.mark.layout
def test_auto_engine(mixed: Path, settings, tmp_path):
    """Default do pacote: reconhece uma vez, pymupdf4llm monta; nenhuma pagina sai com tag HTML."""
    pytest.importorskip("pymupdf.layout")
    out = tmp_path / "auto.md"
    r = process(mixed, None, out, replace(settings, engine="auto"))
    assert r.status == "ok", (r.error_code, r.error_message)
    assert r.engine == "auto" and r.backend == "rapidocr" and r.ocr_done == 2 and r.html_pages <= 2
    text = out.read_text(encoding="utf-8")
    assert "[OCR:" not in text and "pregao eletronico" in text.replace("\n", " ")
    from convert_ocr.core.engines.base import has_html
    assert not has_html(mdfile.parse(text).pages[1].body) and not has_html(mdfile.parse(text).pages[3].body)
    assert "html_pages" in r.to_dict()


@pytest.mark.layout
def test_pymupdf4llm_engine(mixed: Path, settings, tmp_path):
    pytest.importorskip("pymupdf.layout")
    out = tmp_path / "p4llm.md"
    r = process(mixed, None, out, replace(settings, engine="pymupdf4llm"))
    assert r.status == "ok", (r.error_code, r.error_message)
    assert r.engine == "pymupdf4llm" and r.backend == "rapidocr" and r.ocr_done == 2
    text = out.read_text(encoding="utf-8")
    assert "[OCR:" not in text and "pregao eletronico" in text.replace("\n", " ")
