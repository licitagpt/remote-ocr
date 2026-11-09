"""Aplicacao de validacao: varredura do storage, API em processo, execucao de um motor em background e leitura
do resultado por pagina (o .md original nao muda)."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from convert_ocr.review import corpus
from convert_ocr.review.server import Handler, ReviewApp


def test_scan_finds_docs_with_ocr(storage: Path):
    sc = corpus.Scan(storage)
    sc.start()
    for _ in range(200):
        if sc.status != "running":
            break
        time.sleep(0.05)
    assert sc.status == "done", sc.error
    rels = sorted(d.rel for d in sc.docs)
    assert rels == ["files/pncp/2025/12345678000199/000001/blank.pdf", "files/pncp/2025/12345678000199/000001/mixed.pdf",
                    "files/pncp/2025/12345678000199/000001/scan.pdf"]
    mixed = sc.find("files/pncp/2025/12345678000199/000001/mixed.pdf")
    assert mixed.pages == 5 and mixed.ocr_pages == 2 and mixed.pncp == "2025/12345678000199/000001"


def test_original_pages_kinds(storage: Path):
    pages = corpus.original_pages(storage, "files/pncp/2025/12345678000199/000001/mixed.pdf")
    assert [p["kind"] for p in pages] == ["text", "ocr", "text", "ocr", "text"]
    assert pages[1]["body"] == "[OCR:1]"


@pytest.fixture
def api(storage: Path, settings):
    app = ReviewApp(settings, storage / "review" / "ocr")
    Handler.app = app
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if resp.headers.get("Content-Type", "").startswith("application/json") else raw)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    yield call
    httpd.shutdown()
    httpd.server_close()


def _wait_scan(call):
    for _ in range(200):
        st, d = call("GET", "/api/state")
        if d["scan"]["status"] != "running":
            return d
        time.sleep(0.05)
    raise AssertionError("varredura nao terminou")


def test_api_state_docs_and_page(api):
    d = _wait_scan(api)
    assert d["version"] and d["engines"] == ["auto", "rapidocr", "pymupdf4llm"] and d["scan"]["docs"] == 3
    st, docs = api("GET", "/api/docs?q=mixed")
    assert st == 200 and docs["total"] == 1 and docs["docs"][0]["ocr_pages"] == 2 and docs["docs"][0]["results"] == {}
    rel = docs["docs"][0]["rel"]
    st, doc = api("GET", f"/api/doc?pdf={rel}")
    assert st == 200 and len(doc["pages"]) == 5 and doc["results"] == {} and doc["pages"][1]["kind"] == "ocr"
    st, png = api("GET", f"/api/page.png?pdf={rel}&page=2")
    assert st == 200 and png[:8] == b"\x89PNG\r\n\x1a\n"
    assert api("GET", f"/api/page.png?pdf={rel}&page=9")[0] == 400
    assert api("GET", "/api/doc?pdf=files/nada.pdf")[0] == 404
    assert api("POST", "/api/run", {"pdf": rel, "engine": "xyz"})[0] == 400
    assert api("GET", "/api/rota/estranha")[0] == 404
    assert api("GET", "/").__getitem__(0) == 200


@pytest.mark.ocr
def test_api_run_engine_in_background(api, storage: Path, mixed: Path):
    _wait_scan(api)
    rel = mixed.relative_to(storage).as_posix()
    original = mixed.with_suffix(".md").read_text(encoding="utf-8")
    st, run = api("POST", "/api/run", {"pdf": rel, "engine": "rapidocr"})
    assert st == 202 and run["status"] == "running"
    st, again = api("POST", "/api/run", {"pdf": rel, "engine": "rapidocr"})
    assert st == 409                                             # uma execucao por vez
    for _ in range(600):
        st, d = api("GET", "/api/run")
        if d["run"]["status"] != "running":
            break
        time.sleep(0.2)
    assert d["run"]["status"] == "done", d["run"]["error"]
    assert d["run"]["result"]["ocr_done"] == 2
    st, doc = api("GET", f"/api/doc?pdf={rel}")
    res = doc["results"]["rapidocr"]
    assert res["ok"] and res["has_md"] and set(res["bodies"]) == {"1", "2", "3", "4", "5"}
    assert "pregao eletronico" in res["bodies"]["2"].replace("\n", " ") and "[OCR:" not in res["bodies"]["2"]
    assert mixed.with_suffix(".md").read_text(encoding="utf-8") == original     # original intacto
    st, docs = api("GET", "/api/docs?q=mixed")
    assert docs["docs"][0]["results"]["rapidocr"]["ok"] is True
    md_out, json_out = corpus.result_paths(storage / "review" / "ocr", "rapidocr", rel)
    assert md_out.is_file() and json_out.is_file()


# ---------------------------------------------------------------------- resultados gravados fora da tela + lotes
def fake_result(storage: Path, out_dir: Path, engine: str, rel: str, layout: str = "files", with_pdf_field: bool = True,
                ok: bool = True) -> tuple[Path, Path]:
    """Simula `convert_ocr local ... --out <out_dir>/<engine>`: o .md com as paginas [OCR:n] trocadas por texto do
    motor e o .json do resultado. `layout` = "files" (<engine>/files/pncp/...) ou "pncp" (<engine>/pncp/...)."""
    from convert_ocr.core import mdfile
    pdf = storage / rel
    d = mdfile.parse(pdf.with_suffix(".md").read_text(encoding="utf-8"))
    bodies = {p.page: f"texto do {engine} na pagina {p.page}\n\n## Titulo {p.page}" for p in d.ocr_pages}
    sub = Path(rel) if layout == "files" else Path(rel).relative_to("files")
    base = out_dir / engine / sub.with_suffix("")
    base.parent.mkdir(parents=True, exist_ok=True)
    md_out, json_out = base.with_suffix(".md"), base.with_suffix(".json")
    if ok:
        md_out.write_text(mdfile.rebuild(d, bodies), encoding="utf-8")
    info = {"status": "ok" if ok else "error", "ok": ok, "engine": engine, "backend": engine, "pages": d.total,
            "ocr_pages": len(d.ocr_pages), "ocr_done": len(d.ocr_pages) if ok else 0, "hf_removed": 1, "tables": 0,
            "headings": len(bodies), "empty_pages": 0, "rotated_pages": 0, "low_score_pages": 0,
            "ms": {"total": 1200, "ocr": 1000}, "at": "2026-10-24 10:00:00"}
    if with_pdf_field:
        info["pdf"] = str(pdf)
    if not ok:
        info["error"] = {"code": "OCR_FAILED", "message": "motor caiu"}
    json_out.write_text(json.dumps(info), encoding="utf-8")
    return md_out, json_out


REL_MIXED = "files/pncp/2025/12345678000199/000001/mixed.pdf"
REL_SCAN = "files/pncp/2025/12345678000199/000001/scan.pdf"
REL_BLANK = "files/pncp/2025/12345678000199/000001/blank.pdf"


def _scan_done(sc: corpus.Scan) -> corpus.Scan:
    sc.start()
    sc.wait(60)
    assert sc.status == "done", sc.error
    return sc


def test_scan_discovers_results_in_both_layouts(storage: Path):
    out = storage / "review" / "ocr"
    fake_result(storage, out, "rapidocr", REL_MIXED, layout="files")
    fake_result(storage, out, "easyocr", REL_MIXED, layout="pncp", with_pdf_field=False)     # motor de fora, sem campo pdf
    fake_result(storage, out, "easyocr", REL_SCAN, layout="pncp", with_pdf_field=False, ok=False)
    (out / "lotes").mkdir()                                                                  # pasta reservada: nao e motor
    sc = _scan_done(corpus.Scan(storage, out))
    assert list(sc.results) == ["rapidocr", "easyocr"]                    # ENGINES primeiro, depois os outros
    assert sc.engines == ["rapidocr", "easyocr", "auto", "pymupdf4llm"]
    assert sc.result("rapidocr", REL_MIXED).ok and sc.result("easyocr", REL_MIXED).ok
    assert sc.result("easyocr", REL_SCAN).ok is False and sc.result("easyocr", REL_SCAN).status == "error"
    assert corpus.result_bodies(sc.result("easyocr", REL_MIXED))[2].startswith("texto do easyocr na pagina 2")
    assert sc.state()["results"] == {"rapidocr": {"docs": 1, "ok": 1}, "easyocr": {"docs": 2, "ok": 1}}


def test_scan_finds_ocr_md_results(storage: Path):
    """`.ocr.md` ao lado do PDF (saida default do `local`) e `.ocr.md`/`.md` sem .json dentro da pasta do motor."""
    from convert_ocr.core import mdfile
    out = storage / "review" / "ocr"
    orig = (storage / REL_MIXED).with_suffix(".md")
    d = mdfile.parse(orig.read_text(encoding="utf-8"))
    side_md = orig.with_name("mixed" + ".ocr.md")
    side_md.write_text(mdfile.rebuild(d, {p.page: f"ao lado pagina {p.page}" for p in d.ocr_pages}), encoding="utf-8")
    # pasta do motor: so o markdown como .ocr.md (sem .json) e, para outro doc, .md + .ocr.md (o .ocr.md vale)
    eng = out / "tesseract" / "pncp" / "2025" / "12345678000199" / "000001"
    eng.mkdir(parents=True)
    (eng / "mixed.ocr.md").write_text(mdfile.rebuild(d, {p.page: f"tesseract pagina {p.page}" for p in d.ocr_pages}), encoding="utf-8")
    ds = mdfile.parse((storage / REL_SCAN).with_suffix(".md").read_text(encoding="utf-8"))
    (eng / "scan.md").write_text(mdfile.rebuild(ds), encoding="utf-8")                  # copia do original (com [OCR:n])
    (eng / "scan.ocr.md").write_text(mdfile.rebuild(ds, {p.page: "scan tesseract" for p in ds.ocr_pages}), encoding="utf-8")
    (eng / "lixo.md").write_text("sem marcador de pagina", encoding="utf-8")            # nao e saida do convert_pdf
    fake_result(storage, out, "rapidocr", REL_MIXED)
    sc = _scan_done(corpus.Scan(storage, out))
    assert sc.engines == ["rapidocr", "tesseract", "auto", "pymupdf4llm", "ocr-md"]    # o .ocr.md ao lado por ultimo
    assert sorted(d.rel for d in sc.docs) == [REL_BLANK, REL_MIXED, REL_SCAN]           # mixed.ocr.md nao vira documento
    side = sc.result("ocr-md", REL_MIXED)
    assert side.ok and side.json is None and side.md == side_md and side.pages == 5 and side.ocr_pages == 0
    assert corpus.result_bodies(side)[4] == "ao lado pagina 4"
    assert corpus.load_info(side)["no_json"] is True and corpus.load_info(side)["pages"] == 5
    t = sc.results["tesseract"]
    assert set(t) == {REL_MIXED, REL_SCAN} and t[REL_SCAN].md.name == "scan.ocr.md" and t[REL_MIXED].json is None
    assert corpus.result_bodies(t[REL_SCAN])[2] == "scan tesseract"
    # sorteio: sem .json o peso vem das paginas [OCR:n] do .md original
    items, universe = corpus.sample_pages(storage, sc.results, ["tesseract", "ocr-md"], 10, seed=3)
    assert universe == {"docs": 1, "pages": 2} and [(it["pdf"], it["page"]) for it in items] == [(REL_MIXED, 2), (REL_MIXED, 4)]
    assert items[0]["pages"] == 5


def test_api_doc_and_lote_with_ocr_md(api, storage: Path):
    from convert_ocr.core import mdfile
    orig = (storage / REL_SCAN).with_suffix(".md")
    d = mdfile.parse(orig.read_text(encoding="utf-8"))
    orig.with_name("scan.ocr.md").write_text(mdfile.rebuild(d, {p.page: f"lado {p.page}" for p in d.ocr_pages}), encoding="utf-8")
    fake_result(storage, storage / "review" / "ocr", "rapidocr", REL_SCAN)
    api("POST", "/api/scan")
    st = _wait_scan(api)
    assert st["side_engine"] == "ocr-md" and st["engines"][-1] == "ocr-md" and st["scan"]["results"]["ocr-md"] == {"docs": 1, "ok": 1}
    _, doc = api("GET", f"/api/doc?pdf={REL_SCAN}")
    assert doc["results"]["ocr-md"]["no_json"] and doc["results"]["ocr-md"]["bodies"]["3"] == "lado 3"
    _, lote = api("POST", "/api/lotes", {"size": 2, "seed": 5, "engines": ["rapidocr", "ocr-md"]})
    assert lote["total"] == 2 and lote["universe"] == {"docs": 1, "pages": 3}
    _, item = api("GET", "/api/lotes/1/items/0")
    assert item["results"]["ocr-md"]["md"] == f"lado {item['item']['page']}" and item["results"]["ocr-md"]["no_json"]
    assert api("POST", "/api/lotes/1/items/0/verdict", {"best": "ocr-md"})[1]["lote"]["tally"]["ocr-md"] == 1


def test_ocr_pages_fallback_when_md_was_replaced(storage: Path):
    assert corpus.ocr_page_numbers(storage, REL_MIXED) == [2, 4]
    md = storage / Path(REL_MIXED).with_suffix(".md")
    md.write_text(md.read_text(encoding="utf-8").replace("[OCR:1]", "texto").replace("[OCR:2]", "texto"), encoding="utf-8")
    assert corpus.ocr_page_numbers(storage, REL_MIXED) == [2, 4]         # paginas sem camada de texto no PDF
    out = storage / "review" / "ocr"
    fake_result(storage, out, "rapidocr", REL_SCAN)
    sc = _scan_done(corpus.Scan(storage, out))
    rels = {d.rel: d for d in sc.docs}
    assert REL_MIXED not in rels and rels[REL_SCAN].has_markers
    md.unlink()
    sc = _scan_done(corpus.Scan(storage, out))
    assert REL_MIXED not in {d.rel for d in sc.docs}                        # sem .md nem resultado: fora da lista
    fake_result(storage, out, "rapidocr", REL_SCAN)
    md.write_text("", encoding="utf-8")
    fake_result_md = out / "rapidocr" / Path(REL_MIXED).with_suffix(".md")
    fake_result_md.parent.mkdir(parents=True, exist_ok=True)
    fake_result_md.write_text("> __Página 1 de 5__\n\ntexto\n", encoding="utf-8")
    fake_result_md.with_suffix(".json").write_text(json.dumps({"status": "ok", "ok": True, "pages": 5, "ocr_pages": 2,
                                                                "pdf": str(storage / REL_MIXED)}), encoding="utf-8")
    sc = _scan_done(corpus.Scan(storage, out))
    d = {x.rel: x for x in sc.docs}[REL_MIXED]                             # entra pelo resultado, sem marcadores
    assert d.has_markers is False and d.ocr_pages == 2 and d.pages == 5


def test_sample_pages_is_uniform_and_reproducible(storage: Path):
    out = storage / "review" / "ocr"
    for eng in ("rapidocr", "pymupdf4llm"):
        for rel in (REL_MIXED, REL_SCAN, REL_BLANK):
            fake_result(storage, out, eng, rel)
    fake_result(storage, out, "pymupdf4llm", REL_SCAN, ok=False)          # scan.pdf falhou num motor: fora do universo
    sc = _scan_done(corpus.Scan(storage, out))
    items, universe = corpus.sample_pages(storage, sc.results, ["rapidocr", "pymupdf4llm"], 10, seed=7)
    assert universe == {"docs": 2, "pages": 3}                            # mixed (2 paginas) + blank (1)
    assert [(it["pdf"], it["page"]) for it in items] == [(REL_BLANK, 1), (REL_MIXED, 2), (REL_MIXED, 4)]
    assert items[0]["id"] == 0 and items[1]["pncp"] == "2025/12345678000199/000001" and items[1]["ocr_pages"] == 2
    items2, _ = corpus.sample_pages(storage, sc.results, ["rapidocr"], 2, seed=7)
    items3, _ = corpus.sample_pages(storage, sc.results, ["rapidocr"], 2, seed=7)
    assert items2 == items3 and len(items2) == 2
    # so rapidocr: scan.pdf (3 paginas) entra; com 4 paginas pedidas, no maximo 3 por documento enquanto houver outro
    items4, universe4 = corpus.sample_pages(storage, sc.results, ["rapidocr"], 4, seed=1)
    assert universe4 == {"docs": 3, "pages": 6} and len(items4) == 4
    assert max(sum(1 for it in items4 if it["pdf"] == rel) for rel in (REL_MIXED, REL_SCAN, REL_BLANK)) <= 3


def test_api_lotes_roundtrip(api, storage: Path):
    out = storage / "review" / "ocr"
    for eng in ("rapidocr", "pymupdf4llm"):
        for rel in (REL_MIXED, REL_SCAN):
            fake_result(storage, out, eng, rel, layout="files" if eng == "rapidocr" else "pncp")
    api("POST", "/api/scan")
    d = _wait_scan(api)
    assert d["engines"] == ["rapidocr", "pymupdf4llm", "auto"] and d["lotes"] == [] and d["verdict_other"] == ["empate", "nenhum"]
    assert d["scan"]["results"]["pymupdf4llm"] == {"docs": 2, "ok": 2}
    st, docs = api("GET", "/api/docs?q=scan")
    assert docs["docs"][0]["results"]["rapidocr"]["ok"] and docs["docs"][0]["results"]["pymupdf4llm"]["ok"]
    assert api("GET", "/api/docs")[1]["total"] == 3 and api("GET", "/api/docs?with_results=1")[1]["total"] == 2
    st, doc = api("GET", f"/api/doc?pdf={REL_SCAN}")
    assert st == 200 and doc["results"]["pymupdf4llm"]["bodies"]["3"].startswith("texto do pymupdf4llm na pagina 3")

    assert api("POST", "/api/lotes", {"size": 0})[0] == 400
    assert api("POST", "/api/lotes", {"engines": ["easyocr"]})[0] == 400
    st, lote = api("POST", "/api/lotes", {"name": "piloto", "size": 4, "seed": 42, "engines": ["rapidocr", "pymupdf4llm"]})
    assert st == 201 and lote["id"] == 1 and lote["name"] == "piloto" and lote["total"] == 4 and lote["reviewed"] == 0
    assert lote["universe"] == {"docs": 2, "pages": 5} and lote["engines"] == ["rapidocr", "pymupdf4llm"]
    st, full = api("GET", "/api/lotes/1")
    assert st == 200 and len(full["items"]) == 4 and full["stats"]["pending"] == 4
    assert (storage / "review" / "ocr" / "lotes.json").is_file()

    it = full["items"][0]
    st, item = api("GET", f"/api/lotes/1/items/{it['id']}")
    assert st == 200 and item["index"] == 0 and item["original"]["kind"] == "ocr"
    assert item["results"]["rapidocr"]["ok"] and item["results"]["rapidocr"]["md"] == f"texto do rapidocr na pagina {it['page']}\n\n## Titulo {it['page']}"
    assert item["results"]["pymupdf4llm"]["md"].startswith("texto do pymupdf4llm")
    assert item["results"]["rapidocr"]["ms"]["total"] == 1200

    st, v = api("POST", f"/api/lotes/1/items/{it['id']}/verdict", {"best": "rapidocr", "note": "tabela melhor"})
    assert st == 200 and v["item"]["verdict"]["best"] == "rapidocr" and v["lote"]["reviewed"] == 1 and v["lote"]["tally"]["rapidocr"] == 1
    assert api("POST", f"/api/lotes/1/items/{it['id']}/verdict", {"best": "tesseract"})[0] == 400
    api("POST", f"/api/lotes/1/items/{full['items'][1]['id']}/verdict", {"best": "empate"})
    st, full = api("GET", "/api/lotes/1")
    assert full["stats"]["tally"] == {"rapidocr": 1, "pymupdf4llm": 0, "empate": 1, "nenhum": 0}
    assert full["stats"]["notes"] == [{"item_id": it["id"], "pncp": it["pncp"], "file": it["file"], "page": it["page"],
                                       "best": "rapidocr", "note": "tabela melhor"}]
    st, v = api("POST", f"/api/lotes/1/items/{it['id']}/verdict", {"best": None, "note": ""})
    assert "verdict" not in v["item"] and v["lote"]["reviewed"] == 1
    st, d = api("GET", "/api/state")
    assert d["lotes"][0]["reviewed"] == 1
    assert api("DELETE", "/api/lotes/9")[0] == 404
    assert api("DELETE", "/api/lotes/1") == (200, {"deleted": 1})
    assert api("GET", "/api/lotes")[1] == {"lotes": []}
