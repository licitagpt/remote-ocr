"""HTTP da validacao do OCR (biblioteca padrao): API JSON + pagina unica (static/index.html).

GET  /                                   interface
GET  /api/state                          versao, motores, storage, estado da varredura, execucao em andamento, lotes
POST /api/scan                           (re)varre o storage e os resultados em background
GET  /api/docs?q=&limit=&offset=&with_results=1   documentos (com [OCR:n] no .md ou com resultado gravado), filtro por texto;
                                         with_results=1 so os que tem resultado gravado em algum motor
GET  /api/doc?pdf=<rel>                  paginas do .md atual (tipo + corpo) e, por motor, resultado (.json) e corpos
POST /api/run                            {pdf, engine} -> roda o OCR do documento em background (resultado em <out_dir>/<motor>/…)
GET  /api/run                            estado da execucao atual (progresso, erro descritivo)
GET  /api/page.png?pdf=<rel>&page=<n>    imagem da pagina do PDF

GET  /api/lotes                          lotes de revisao (resumo: tamanho, revisadas, contagem por veredito)
POST /api/lotes                          {name, size, seed, engines:[...]} -> sorteia N paginas de OCR entre os documentos
                                         com resultado ok em todos os motores escolhidos
GET  /api/lotes/<id>                     lote com itens
DELETE /api/lotes/<id>
GET  /api/lotes/<id>/items/<i>           item: pagina do .md atual + corpo da pagina em cada motor + metricas do documento
POST /api/lotes/<id>/items/<i>/verdict   {best: motor|empate|nenhum|null, note}

O .md original nunca e tocado pela validacao: cada motor grava a sua copia em <out_dir>/<motor>/.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import traceback
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from convert_ocr import __version__
from convert_ocr.config import ENGINES, Settings
from convert_ocr.review import corpus
from convert_ocr.review.store import VERDICT_OTHER, Store, lote_stats, lote_summary

log = logging.getLogger("convert_ocr.review")
STATIC = Path(__file__).with_name("static")
MAX_LOTE = 2000


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class ReviewApp:
    def __init__(self, settings: Settings, out_dir: Path):
        self.settings = settings
        self.storage = Path(settings.storage_dir)
        self.out_dir = Path(out_dir)
        self.store = Store(self.out_dir / "lotes.json")
        self.scan = corpus.Scan(self.storage, self.out_dir)
        self.scan.start()
        self._run_lock = threading.Lock()
        self.run: dict[str, Any] | None = None       # execucao atual/ultima: {pdf, engine, status, progress, result, error}
        self._engine_inited = False
        self._bodies_cache: dict[tuple[str, str], tuple[float, dict[int, str]]] = {}
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------------ estado
    def state(self) -> dict[str, Any]:
        from convert_ocr.core.device import probe
        return {"version": __version__, "storage": str(self.storage), "out_dir": str(self.out_dir),
                "engines": self.scan.engines, "run_engines": list(ENGINES), "default_engine": self.settings.engine,
                "backend": self.settings.ocr_backend, "device": probe(self.settings).to_dict(),
                "dpi": self.settings.ocr_dpi, "scan": self.scan.state(), "run": self.run_state(),
                "lotes": [lote_summary(b) for b in self.store.lotes], "verdict_other": list(VERDICT_OTHER),
                "side_engine": corpus.SIDE_ENGINE}

    def run_state(self) -> dict[str, Any] | None:
        return dict(self.run) if self.run else None

    # ------------------------------------------------------------------ documentos
    def docs(self, q: str, limit: int, offset: int, with_results: bool = False) -> dict[str, Any]:
        docs = self.scan.docs
        if with_results:
            have = set().union(*(set(r) for r in self.scan.results.values())) if self.scan.results else set()
            docs = [d for d in docs if d.rel in have]
        if q:
            terms = q.lower().split()
            docs = [d for d in docs if all(t in (d.rel + " " + d.pncp).lower() for t in terms)]
        page = docs[offset:offset + limit]
        return {"total": len(docs), "offset": offset,
                "docs": [{**asdict(d), "results": self._results_summary(d.rel)} for d in page]}

    def _results_summary(self, rel: str) -> dict[str, Any]:
        out = {}
        for eng in self.scan.engines:
            r = self.scan.result(eng, rel)
            if r:
                info = corpus.load_info(r)
                out[eng] = {"ok": info.get("ok"), "status": info.get("status"), "at": info.get("at"),
                            "ms": (info.get("ms") or {}).get("total"), "error": (info.get("error") or {}).get("message")}
        return out

    def _result_info(self, eng: str, rel: str) -> dict[str, Any] | None:
        r = self.scan.result(eng, rel)
        return corpus.load_info(r) if r else None

    def _bodies(self, eng: str, rel: str) -> dict[int, str]:
        r = self.scan.result(eng, rel)
        if not r or not r.md.is_file():
            return {}
        try:
            mtime = r.md.stat().st_mtime
        except OSError:
            return {}
        key = (eng, rel)
        with self._cache_lock:
            hit = self._bodies_cache.get(key)
            if hit and hit[0] == mtime:
                return hit[1]
        bodies = corpus.result_bodies(r)
        with self._cache_lock:
            if len(self._bodies_cache) > 64:
                self._bodies_cache.clear()
            self._bodies_cache[key] = (mtime, bodies)
        return bodies

    def doc(self, rel: str) -> dict[str, Any]:
        pdf = corpus.storage_path(self.storage, rel)
        if not pdf.is_file():
            raise ApiError(404, f"PDF nao existe: {rel}")
        pages = corpus.original_pages(self.storage, rel)
        results: dict[str, Any] = {}
        for eng in self.scan.engines:
            info = self._result_info(eng, rel)
            if info:
                info["bodies"] = self._bodies(eng, rel)
                results[eng] = info
        if not pages:
            # .md original ausente ou sem marcadores de pagina: monta a lista a partir do PDF e de um resultado
            n = corpus.page_count(pdf)
            if not n:
                raise ApiError(404, f"o .md do convert_pdf nao existe ou nao tem paginas: {rel}")
            ocr = set(corpus.ocr_page_numbers(self.storage, rel))
            pages = [{"page": i, "kind": "ocr" if i in ocr else "text", "body": None} for i in range(1, n + 1)]
        elif results and not any(p["kind"] == "ocr" for p in pages):
            # .md ja substituido (sem [OCR:n]) mas com resultado gravado: as paginas de OCR vem do PDF
            ocr = set(corpus.ocr_page_numbers(self.storage, rel))
            for p in pages:
                if p["page"] in ocr:
                    p["kind"] = "ocr"
        d = self.scan.find(rel)
        return {"pdf": rel, "file": pdf.name, "pncp": d.pncp if d else corpus.doc_from_rel(self.storage, rel)[0],
                "has_markers": d.has_markers if d else bool(pages and pages[0]["body"] is not None),
                "pages": pages, "results": results}

    # ------------------------------------------------------------------ execucao
    def start_run(self, rel: str, engine: str) -> dict[str, Any]:
        if engine not in ENGINES:
            raise ApiError(400, f"engine deve ser {'|'.join(ENGINES)}")
        pdf = corpus.storage_path(self.storage, rel)
        if not pdf.is_file():
            raise ApiError(404, f"PDF nao existe: {rel}")
        if not pdf.with_suffix(".md").is_file():
            raise ApiError(404, f"o .md do convert_pdf nao existe ao lado de {rel}")
        if not self._run_lock.acquire(blocking=False):
            cur = self.run or {}
            raise ApiError(409, f"ja ha um OCR em andamento: {cur.get('pdf')} ({cur.get('engine')}); aguarde")
        self.run = {"pdf": rel, "engine": engine, "status": "running", "progress": "iniciando", "started_at": time.time(),
                    "result": None, "error": ""}
        threading.Thread(target=self._do_run, args=(rel, engine, pdf), name="ocr-review-run", daemon=True).start()
        return self.run_state()

    def _do_run(self, rel: str, engine: str, pdf: Path) -> None:
        try:
            from convert_ocr.core.document import init_process, process
            if not self._engine_inited:
                init_process(self.settings)
                self._engine_inited = True
            prev = self.scan.result(engine, rel)
            if prev:                                             # regrava onde o resultado anterior deste motor esta
                md_out, json_out = prev.md, prev.json or corpus.result_base(prev.md).with_suffix(".json")
            else:
                md_out, json_out = corpus.result_paths(self.out_dir, engine, rel)

            def progress(done: int, total: int, page: int, ms: float) -> None:
                self.run["progress"] = f"pagina {page}: {done}/{total} ({ms / 1000:.1f}s)"

            self.run["progress"] = "carregando o motor"
            r = process(pdf, None, md_out, self.settings, engine=engine, progress=progress)
            info = r.to_dict()
            info["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            info["version"] = __version__
            json_out.parent.mkdir(parents=True, exist_ok=True)
            json_out.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
            if not r.ok and md_out.is_file():
                md_out.unlink()                                  # resultado velho nao representa esta execucao
            self.scan.add_result(corpus.Result(engine=engine, rel=rel, md=md_out, json=json_out, ok=r.ok and md_out.is_file(),
                                               status=r.status, ocr_pages=r.ocr_pages, pages=r.pages))
            self.run.update(status="done" if r.ok else "error", progress="", result=info,
                            error="" if r.ok else f"{r.error_code}: {r.error_message}")
            log.info("review: %s %s -> %s (%d/%d paginas, %.1fs)%s", engine, pdf.name, r.status, r.ocr_done,
                     r.ocr_pages, r.ms.total / 1000, "" if r.ok else f" {r.error_code}: {r.error_message}")
        except Exception as e:  # noqa: BLE001
            log.error("review: %s falhou: %s\n%s", rel, e, traceback.format_exc())
            self.run.update(status="error", progress="", error=f"{type(e).__name__}: {e}")
        finally:
            self._run_lock.release()

    # ------------------------------------------------------------------ lotes
    def create_lote(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.scan.status == "running":
            raise ApiError(409, "a varredura ainda esta em andamento; aguarde")
        size = int(body["size"]) if body.get("size") not in (None, "") else 30
        if not 1 <= size <= MAX_LOTE:
            raise ApiError(400, f"size deve estar entre 1 e {MAX_LOTE}")
        seed = int(body.get("seed") or int(time.time()) % 100000)
        engines = body.get("engines") or list(self.scan.results)
        if not isinstance(engines, list) or not engines:
            raise ApiError(400, "engines deve ser uma lista com ao menos um motor")
        engines = [str(e) for e in engines]
        missing = [e for e in engines if e not in self.scan.results]
        if missing:
            raise ApiError(400, f"sem resultados gravados para: {', '.join(missing)} (rode o convert_ocr com --out "
                                f"{self.out_dir / '<motor>'} antes)")
        items, universe = corpus.sample_pages(self.storage, self.scan.results, engines, size, seed)
        if not items:
            raise ApiError(400, "nenhuma pagina sorteada: nenhum documento tem resultado ok em todos os motores "
                                f"escolhidos ({', '.join(engines)})")
        lote = self.store.new(str(body.get("name") or ""), size, seed, engines, items, __version__, universe)
        log.info("lote %d: %d paginas de %d documentos (universo %d docs / %d paginas; motores %s; seed %d)",
                 lote["id"], len(items), len({it["pdf"] for it in items}), universe["docs"], universe["pages"],
                 ", ".join(engines), seed)
        return lote_summary(lote)

    def get_lote(self, lid: int) -> dict[str, Any]:
        lote = self.store.get(lid)
        if lote is None:
            raise ApiError(404, f"lote {lid} nao existe")
        return lote

    def lote_payload(self, lid: int) -> dict[str, Any]:
        lote = self.get_lote(lid)
        return {**lote, "stats": lote_stats(lote)}

    def delete_lote(self, lid: int) -> None:
        if not self.store.delete(lid):
            raise ApiError(404, f"lote {lid} nao existe")

    def get_item(self, lid: int, item_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        lote = self.get_lote(lid)
        item = next((it for it in lote["items"] if it["id"] == item_id), None)
        if item is None:
            raise ApiError(404, f"item {item_id} nao existe no lote {lid}")
        return lote, item

    def item_payload(self, lid: int, item_id: int) -> dict[str, Any]:
        lote, item = self.get_item(lid, item_id)
        rel, page = item["pdf"], int(item["page"])
        orig = next((p for p in corpus.original_pages(self.storage, rel) if p["page"] == page), None)
        results: dict[str, Any] = {}
        for eng in lote["engines"]:
            info = self._result_info(eng, rel)
            if not info:
                results[eng] = {"ok": False, "error": {"code": "MISSING", "message": "sem resultado gravado"}, "md": None}
                continue
            bodies = self._bodies(eng, rel) if info.get("ok") else {}
            keep = ("ok", "status", "error", "at", "version", "engine", "backend", "ocr_pages", "ocr_done", "empty_pages",
                    "rotated_pages", "low_score_pages", "html_pages", "hf_removed", "tables", "headings", "token_count",
                    "ms", "no_json", "md_path")
            results[eng] = {k: info.get(k) for k in keep} | {"md": bodies.get(page) if info.get("ok") else None}
        return {"lote": lote_summary(lote), "item": item, "index": lote["items"].index(item),
                "original": orig, "results": results}

    def set_verdict(self, lid: int, item_id: int, body: dict[str, Any]) -> dict[str, Any]:
        self.get_item(lid, item_id)
        best = body.get("best")
        try:
            item = self.store.set_verdict(lid, item_id, str(best) if best else None, str(body.get("note") or ""))
        except (KeyError, ValueError) as e:
            raise ApiError(400, str(e)) from e
        return {"item": item, "lote": lote_summary(self.store.get(lid))}

    # ------------------------------------------------------------------ imagem
    def page_png(self, rel: str, page: int) -> bytes:
        pdf = corpus.storage_path(self.storage, rel)
        if not pdf.is_file():
            raise ApiError(404, f"PDF nao existe: {rel}")
        n = corpus.page_count(pdf)
        if not 1 <= page <= n:
            raise ApiError(400, f"pagina {page} fora de 1..{n}")
        return corpus.render_page(pdf, page)


# ---------------------------------------------------------------------- HTTP
_ROUTES = [
    ("GET", re.compile(r"^/$"), "index"),
    ("GET", re.compile(r"^/api/state$"), "state"),
    ("POST", re.compile(r"^/api/scan$"), "scan"),
    ("GET", re.compile(r"^/api/docs$"), "docs"),
    ("GET", re.compile(r"^/api/doc$"), "doc"),
    ("POST", re.compile(r"^/api/run$"), "start_run"),
    ("GET", re.compile(r"^/api/run$"), "run"),
    ("GET", re.compile(r"^/api/page\.png$"), "page"),
    ("GET", re.compile(r"^/api/lotes$"), "lotes"),
    ("POST", re.compile(r"^/api/lotes$"), "create_lote"),
    ("GET", re.compile(r"^/api/lotes/(\d+)$"), "lote"),
    ("DELETE", re.compile(r"^/api/lotes/(\d+)$"), "delete_lote"),
    ("GET", re.compile(r"^/api/lotes/(\d+)/items/(\d+)$"), "item"),
    ("POST", re.compile(r"^/api/lotes/(\d+)/items/(\d+)/verdict$"), "verdict"),
]


def _q(query: dict, name: str, default: str = "") -> str:
    v = query.get(name)
    return v[0] if v else default


class Handler(BaseHTTPRequestHandler):
    app: ReviewApp   # definido em serve()
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:   # noqa: A003
        log.debug("%s " + fmt, self.address_string(), *args)

    def do_GET(self) -> None:     # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:    # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        try:
            for m, rx, name in _ROUTES:
                mt = rx.match(url.path)
                if m == method and mt:
                    getattr(self, "h_" + name)(query, *[int(g) for g in mt.groups()])
                    return
            raise ApiError(404, f"rota desconhecida: {method} {url.path}")
        except ApiError as e:
            self._json({"error": str(e)}, e.status)
        except (KeyError, ValueError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            log.error("%s %s: %s\n%s", method, self.path, e, traceback.format_exc())
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ApiError(400, f"JSON invalido: {e}") from e
        if not isinstance(data, dict):
            raise ApiError(400, "esperado um objeto JSON")
        return data

    def _send(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj: Any, status: int = 200) -> None:
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    # ------------------------------------------------------------------ rotas
    def h_index(self, query: dict) -> None:
        self._send((STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")

    def h_state(self, query: dict) -> None:
        self._json(self.app.state())

    def h_scan(self, query: dict) -> None:
        started = self.app.scan.start()
        self._json({"started": started, "scan": self.app.scan.state()}, HTTPStatus.ACCEPTED if started else 200)

    def h_docs(self, query: dict) -> None:
        limit = max(1, min(500, int(_q(query, "limit", "200"))))
        offset = max(0, int(_q(query, "offset", "0")))
        self._json(self.app.docs(_q(query, "q"), limit, offset, _q(query, "with_results") in ("1", "true")))

    def h_doc(self, query: dict) -> None:
        rel = _q(query, "pdf")
        if not rel:
            raise ApiError(400, "parametro 'pdf' obrigatorio")
        self._json(self.app.doc(rel))

    def h_start_run(self, query: dict) -> None:
        b = self._body()
        rel, engine = str(b.get("pdf") or ""), str(b.get("engine") or self.app.settings.engine)
        if not rel:
            raise ApiError(400, "campo 'pdf' obrigatorio")
        self._json(self.app.start_run(rel, engine), HTTPStatus.ACCEPTED)

    def h_run(self, query: dict) -> None:
        self._json({"run": self.app.run_state()})

    def h_page(self, query: dict) -> None:
        rel = _q(query, "pdf")
        page = int(_q(query, "page", "1"))
        if not rel:
            raise ApiError(400, "parametro 'pdf' obrigatorio")
        self._send(self.app.page_png(rel, page), "image/png")

    def h_lotes(self, query: dict) -> None:
        self._json({"lotes": [lote_summary(b) for b in self.app.store.lotes]})

    def h_create_lote(self, query: dict) -> None:
        self._json(self.app.create_lote(self._body()), HTTPStatus.CREATED)

    def h_lote(self, query: dict, lid: int) -> None:
        self._json(self.app.lote_payload(lid))

    def h_delete_lote(self, query: dict, lid: int) -> None:
        self.app.delete_lote(lid)
        self._json({"deleted": lid})

    def h_item(self, query: dict, lid: int, item_id: int) -> None:
        self._json(self.app.item_payload(lid, item_id))

    def h_verdict(self, query: dict, lid: int, item_id: int) -> None:
        self._json(self.app.set_verdict(lid, item_id, self._body()))


def serve(settings: Settings, out_dir: Path, host: str = "127.0.0.1", port: int = 8766, open_browser: bool = True) -> int:
    app = ReviewApp(settings, out_dir)
    Handler.app = app
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    url = f"http://{host}:{port}/"
    log.info("validacao do OCR em %s | storage=%s | resultados em %s | motor default %s", url, settings.storage_dir,
             out_dir, settings.engine)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        from convert_ocr.core.engines import close_all
        close_all()
    return 0
