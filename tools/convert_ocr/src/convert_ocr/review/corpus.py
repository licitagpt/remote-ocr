"""Corpus da validacao.

Duas fontes, varridas em background:

- documentos de <storage>/files cujo .md (convert_pdf) ainda tem paginas [OCR:n] (candidatos a "Processar");
- resultados ja gravados em <out_dir>/<motor>/... de qualquer execucao anterior: o botao Processar desta tela ou
  `convert_ocr local <dir> --out <out_dir>/<motor>`. Um resultado e `<nome>.md` ou `<nome>.ocr.md` (este tem
  preferencia quando ha os dois) e, se houver, `<nome>.json` com as metricas; sem .json o resultado entra sem metricas.
  O PDF de origem vem do campo `pdf` do .json (ou da posicao do arquivo dentro da pasta do motor), por isso a arvore
  pode ser `files/pncp/...` ou `pncp/...`. Os motores sao as subpastas de <out_dir>: motores conhecidos (ENGINES) e
  tambem outros testados fora daqui;
- `<nome>.ocr.md` ao lado do PDF em <storage>/files (saida default do `convert_ocr local`, sem --out): entra como o
  pseudo-motor SIDE_ENGINE ("ocr-md"), ja que o arquivo nao diz qual motor o gerou.

Mais a imagem da pagina, as paginas de OCR de um documento e o sorteio de paginas para um lote.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf

from convert_ocr.config import ENGINES, OCR_MD_SUFFIX
from convert_ocr.core import mdfile

log = logging.getLogger("convert_ocr.review")
MAX_PER_DOC = 3                 # paginas de um mesmo documento num lote, enquanto houver alternativa
RESERVED_DIRS = {"lotes", "logs"}
SIDE_ENGINE = "ocr-md"          # pseudo-motor: <nome>.ocr.md ao lado do PDF


@dataclass(slots=True)
class Doc:
    rel: str                  # relativo ao storage, posix ("files/pncp/2023/.../x.pdf")
    pncp: str                 # pastas entre files/pncp e o arquivo
    file: str
    pages: int = 0
    ocr_pages: int = 0        # paginas [OCR:n] no .md atual (ou, sem marcadores, as que o resultado diz ter tratado)
    md_bytes: int = 0
    has_markers: bool = True  # o .md ao lado do PDF ainda tem [OCR:n]


@dataclass(slots=True)
class Result:
    """Resultado de um motor para um documento: <out_dir>/<motor>/... ou <nome>.ocr.md ao lado do PDF."""
    engine: str
    rel: str                  # PDF relativo ao storage
    md: Path
    json: Path | None         # None = so o markdown (sem metricas)
    ok: bool = False
    status: str = ""
    ocr_pages: int = 0        # 0 quando nao ha .json (desconhecido)
    pages: int = 0


def result_base(md: Path) -> Path:
    """`x.ocr.md` e `x.md` -> `x` (sem sufixo)."""
    if md.name.endswith(OCR_MD_SUFFIX):
        return md.with_name(md.name[:-len(OCR_MD_SUFFIX)])
    return md.with_suffix("")


def doc_from_rel(storage: Path, rel: str) -> tuple[str, str]:
    """(pncp, nome do arquivo) a partir do caminho relativo ao storage."""
    p = Path(rel)
    parts = p.parts
    if len(parts) > 2 and parts[0] == "files":
        parts = parts[1:]
    pncp = "/".join(parts[1:-1]) if len(parts) > 2 else "/".join(parts[:-1])
    return pncp, p.name


class Scan:
    """Varredura dos .md do storage e dos resultados em background; as listas ficam em memoria."""

    def __init__(self, storage: Path, out_dir: Path | None = None):
        self.storage = Path(storage)
        self.out_dir = Path(out_dir) if out_dir else None
        self.docs: list[Doc] = []
        self.results: dict[str, dict[str, Result]] = {}      # motor -> rel -> Result
        self.status = "idle"          # idle | running | done | error
        self.progress = ""
        self.scanned_at: float | None = None
        self.error = ""
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._again = False           # Revarrer pedido durante uma varredura: repete ao terminar

    # ------------------------------------------------------------------ execucao
    def start(self) -> bool:
        """Inicia a varredura; se uma ja estiver em andamento, pede outra passada ao fim dela (devolve False)."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                self._again = True
                return False
            self._again = False
            self.status, self.progress, self.error = "running", "iniciando", ""
            self._thread = threading.Thread(target=self._run, name="ocr-review-scan", daemon=True)
            self._thread.start()
            return True

    def wait(self, timeout: float = 60.0) -> None:
        t = self._thread
        if t:
            t.join(timeout)

    def _run(self) -> None:
        while True:
            self._run_once()
            with self._lock:
                if not self._again:
                    return
                self._again = False
                self.status, self.progress, self.error = "running", "iniciando", ""

    def _run_once(self) -> None:
        try:
            docs, side = self._scan_storage()
            results = self._scan_results() if self.out_dir else {}
            if side:
                results[SIDE_ENGINE] = side
            by_rel = {d.rel: d for d in docs}
            for eng, per_rel in results.items():
                for rel, r in per_rel.items():
                    if rel in by_rel:
                        continue
                    pdf = self.storage / rel
                    if not pdf.is_file():
                        continue
                    pncp, name = doc_from_rel(self.storage, rel)
                    d = Doc(rel=rel, pncp=pncp, file=name, pages=r.pages, ocr_pages=r.ocr_pages, has_markers=False)
                    by_rel[rel] = d
                    docs.append(d)
            docs.sort(key=lambda d: d.rel)
            with self._lock:
                self.docs, self.results, self.scanned_at = docs, results, time.time()
                if not self._again:                         # senao fica "running" ate a proxima passada
                    self.status, self.progress = "done", ""
            log.info("varredura: %d documentos (%d com [OCR:n] no .md) | resultados: %s", len(docs),
                     sum(1 for d in docs if d.has_markers),
                     ", ".join(f"{e} {len(r)}" for e, r in results.items()) or "nenhum")
        except Exception as e:  # noqa: BLE001
            with self._lock:
                if not self._again:
                    self.status, self.error = "error", f"{type(e).__name__}: {e}"
            log.exception("varredura falhou")

    def _scan_storage(self) -> tuple[list[Doc], dict[str, Result]]:
        """(documentos com [OCR:n] no .md, resultados <nome>.ocr.md ao lado do PDF)."""
        root = self.storage / "files"
        mds = sorted(root.rglob("*.md")) if root.is_dir() else []
        found: list[Doc] = []
        side: dict[str, Result] = {}
        for i, md in enumerate(mds):
            if i % 200 == 0:
                self.progress = f"{i}/{len(mds)} .md lidos, {len(found)} com OCR"
            if md.name.endswith(OCR_MD_SUFFIX):
                pdf = result_base(md).with_suffix(".pdf")
                if pdf.is_file():
                    r = read_result(self.storage, None, md, None, engine=SIDE_ENGINE)
                    if r:
                        side[r.rel] = r
                continue
            pdf = md.with_suffix(".pdf")
            if not pdf.is_file():
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "[OCR:" not in text:
                continue
            try:
                d = mdfile.parse(text)
            except ValueError:
                continue
            n_ocr = len(d.ocr_pages)
            if not n_ocr:
                continue
            rel = pdf.relative_to(self.storage).as_posix()
            pncp, name = doc_from_rel(self.storage, rel)
            found.append(Doc(rel=rel, pncp=pncp, file=name, pages=d.total, ocr_pages=n_ocr,
                             md_bytes=len(text.encode("utf-8"))))
        return found, side

    def _scan_results(self) -> dict[str, dict[str, Result]]:
        out: dict[str, dict[str, Result]] = {}
        if not self.out_dir or not self.out_dir.is_dir():
            return out
        engine_dirs = sorted(p for p in self.out_dir.iterdir() if p.is_dir() and p.name not in RESERVED_DIRS)
        for eng_dir in engine_dirs:
            # um resultado por base: <base>.ocr.md > <base>.md como markdown; <base>.json (opcional) com as metricas
            mds: dict[Path, Path | None] = {}
            for md in sorted(eng_dir.rglob("*.md")):
                base = result_base(md)
                if base not in mds or md.name.endswith(OCR_MD_SUFFIX):
                    mds[base] = md
            for js in eng_dir.rglob("*.json"):
                mds.setdefault(js.with_suffix(""), None)           # .json sem .md: execucao com erro
            per_rel: dict[str, Result] = {}
            for i, (base, md) in enumerate(sorted(mds.items())):
                if i % 200 == 0:
                    self.progress = f"resultados {eng_dir.name}: {i}/{len(mds)}"
                js = base.with_suffix(".json")
                r = read_result(self.storage, eng_dir, md or base.with_suffix(".md"), js if js.is_file() else None)
                if r is not None:
                    per_rel[r.rel] = r
            if per_rel:
                out[eng_dir.name] = per_rel
        # motores conhecidos primeiro, na ordem de ENGINES; depois os outros em ordem alfabetica
        order = {e: i for i, e in enumerate(ENGINES)}
        return dict(sorted(out.items(), key=lambda kv: (order.get(kv[0], len(order)), kv[0])))

    # ------------------------------------------------------------------ consulta
    def state(self) -> dict[str, Any]:
        return {"status": self.status, "progress": self.progress, "error": self.error, "docs": len(self.docs),
                "scanned_at": self.scanned_at,
                "results": {e: {"docs": len(r), "ok": sum(1 for x in r.values() if x.ok)} for e, r in self.results.items()}}

    @property
    def engines(self) -> list[str]:
        """Motores com resultado gravado + os que esta ferramenta sabe rodar (ENGINES), sem repetir; o pseudo-motor
        do .ocr.md ao lado do PDF por ultimo."""
        seen = [e for e in self.results if e != SIDE_ENGINE]
        out = seen + [e for e in ENGINES if e not in seen]
        return out + [SIDE_ENGINE] if SIDE_ENGINE in self.results else out

    def find(self, rel: str) -> Doc | None:
        return next((d for d in self.docs if d.rel == rel), None)

    def result(self, engine: str, rel: str) -> Result | None:
        return self.results.get(engine, {}).get(rel)

    def add_result(self, r: Result) -> None:
        with self._lock:
            self.results.setdefault(r.engine, {})[r.rel] = r
            if not self.find(r.rel):
                pncp, name = doc_from_rel(self.storage, r.rel)
                self.docs.append(Doc(rel=r.rel, pncp=pncp, file=name, pages=r.pages, ocr_pages=r.ocr_pages,
                                     has_markers=False))
                self.docs.sort(key=lambda d: d.rel)


# ---------------------------------------------------------------------- caminhos / resultados
def storage_path(storage: Path, rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else storage / p


def result_paths(out_dir: Path, engine: str, rel: str) -> tuple[Path, Path]:
    """(.md, .json) onde o botao Processar grava o resultado de um documento num motor."""
    base = out_dir / engine / Path(rel).with_suffix("")
    return base.with_suffix(".md"), base.with_suffix(".json")


def _rel_of(storage: Path, eng_dir: Path | None, base: Path, info: dict[str, Any]) -> str | None:
    """PDF (relativo ao storage) a que um resultado se refere: campo `pdf` do .json, senao a posicao do arquivo
    dentro da pasta do motor (<motor>/files/pncp/... ou <motor>/pncp/...); sem pasta de motor (.ocr.md ao lado do
    PDF), o proprio <base>.pdf."""
    storage = storage.resolve()
    cands: list[Path] = []
    pdf = info.get("pdf")
    if pdf:
        p = Path(pdf)
        cands.append(p if p.is_absolute() else storage / p)
    if eng_dir is None:
        rel_json = base.with_suffix(".pdf")
        cands.append(rel_json)
    else:
        rel_json = base.relative_to(eng_dir).with_suffix(".pdf")
        cands += [storage / rel_json, storage / "files" / rel_json]
    for c in cands:
        try:
            rc = c.resolve()
        except OSError:
            continue
        if rc.is_file():
            try:
                return rc.relative_to(storage).as_posix()
            except ValueError:
                return rc.as_posix()
    if eng_dir is None:
        return None
    # PDF nao esta nesta maquina: ainda assim registra pelo caminho relativo mais provavel
    return ("files" / rel_json).as_posix() if rel_json.parts and rel_json.parts[0] != "files" else rel_json.as_posix()


def _md_only_info(md: Path) -> dict[str, Any] | None:
    """Metricas minimas de um resultado que so tem o markdown (sem .json)."""
    try:
        d = mdfile.parse(md.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {"status": "ok", "ok": True, "pages": d.total, "ocr_pages": 0, "no_json": True}


def read_result(storage: Path, eng_dir: Path | None, md: Path, json_path: Path | None,
                engine: str | None = None) -> Result | None:
    """Resultado a partir do markdown (`x.md` | `x.ocr.md`) e, se houver, do `.json` com as metricas."""
    if json_path is not None:
        try:
            info = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(info, dict) or "status" not in info:
            return None
    else:
        info = _md_only_info(md)
        if info is None:
            return None
    rel = _rel_of(storage, eng_dir, result_base(md), info)
    if not rel:
        return None
    return Result(engine=engine or (eng_dir.name if eng_dir else SIDE_ENGINE), rel=rel, md=md, json=json_path,
                  ok=bool(info.get("ok")) and md.is_file(), status=str(info.get("status") or ""),
                  ocr_pages=int(info.get("ocr_pages") or 0), pages=int(info.get("pages") or 0))


def load_info(r: Result) -> dict[str, Any]:
    if r.json is None:
        info = _md_only_info(r.md) or {"ok": False, "status": "error",
                                       "error": {"code": "BAD_MD", "message": f"markdown ilegivel: {r.md.name}"}}
        info["md_path"] = str(r.md)
    else:
        try:
            info = json.loads(r.json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return {"ok": False, "status": "error", "error": {"code": "IO", "message": f"resultado ilegivel: {e}"}}
    info["has_md"] = r.md.is_file()
    return info


def result_bodies(r: Result) -> dict[int, str]:
    if not r.md.is_file():
        return {}
    try:
        d = mdfile.parse(r.md.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {p.page: p.body for p in d.pages}


def original_pages(storage: Path, rel: str) -> list[dict[str, Any]]:
    """Paginas do .md atual (ao lado do PDF): numero, tipo e corpo."""
    md = storage_path(storage, rel).with_suffix(".md")
    if not md.is_file():
        return []
    try:
        d = mdfile.parse(md.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for p in d.pages:
        b = p.body
        kind = "ocr" if p.ocr_n is not None else "blank" if b in ("", "[BLANK]") else "draw" if b.startswith("[DRAW:") else "text"
        out.append({"page": p.page, "kind": kind, "body": b})
    return out


def ocr_page_numbers(storage: Path, rel: str) -> list[int]:
    """Paginas de OCR de um documento: as [OCR:n] do .md atual; se o .md ja foi substituido (sem marcadores) ou
    nao existe, as paginas do PDF sem camada de texto."""
    pages = original_pages(storage, rel)
    if pages and any(p["kind"] == "ocr" for p in pages):
        return [p["page"] for p in pages if p["kind"] == "ocr"]
    pdf = storage_path(storage, rel)
    if not pdf.is_file():
        return []
    try:
        with pymupdf.open(str(pdf)) as doc:
            return [i + 1 for i, pg in enumerate(doc) if not pg.get_text("text").strip()]
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------- sorteio
def sample_pages(storage: Path, results: dict[str, dict[str, Result]], engines: list[str], size: int, seed: int,
                 ocr_pages_of=None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Sorteia `size` paginas de OCR, uniformemente entre todas as paginas de OCR dos documentos que tem resultado
    ok em TODOS os `engines`, com no maximo MAX_PER_DOC por documento enquanto houver alternativa. Devolve os itens
    (ordenados por documento e pagina) e o tamanho do universo ({docs, pages})."""
    ocr_pages_of = ocr_pages_of or (lambda rel: ocr_page_numbers(storage, rel))
    rels = None
    for e in engines:
        ok = {rel for rel, r in results.get(e, {}).items() if r.ok}
        rels = ok if rels is None else rels & ok
    rels = sorted(rels or [])
    cache: dict[str, list[int]] = {}

    def pages_of(rel: str) -> list[int]:
        if rel not in cache:
            cache[rel] = ocr_pages_of(rel)
        return cache[rel]

    # peso = paginas de OCR do documento: do .json; resultado so com markdown (sem .json) nao sabe, conta no .md/PDF
    weights = {rel: max((results[e][rel].ocr_pages for e in engines), default=0) or len(pages_of(rel)) for rel in rels}
    pool = [(rel, k) for rel in rels for k in range(weights[rel])]
    rnd = random.Random(seed)
    rnd.shuffle(pool)

    def page_of(rel: str, k: int) -> int | None:
        lst = pages_of(rel)
        return lst[k] if k < len(lst) else None

    chosen: list[tuple[str, int]] = []
    taken: set[tuple[str, int]] = set()
    per_doc: dict[str, int] = {}
    for cap in (MAX_PER_DOC, None):
        for rel, k in pool:
            if len(chosen) >= size:
                break
            if cap is not None and per_doc.get(rel, 0) >= cap:
                continue
            page = page_of(rel, k)
            if page is None or (rel, page) in taken:
                continue
            taken.add((rel, page))
            per_doc[rel] = per_doc.get(rel, 0) + 1
            chosen.append((rel, page))
    chosen.sort()
    items = []
    for i, (rel, page) in enumerate(chosen):
        pncp, name = doc_from_rel(storage, rel)
        items.append({"id": i, "pdf": rel, "pncp": pncp, "file": name, "page": page,
                      "ocr_pages": weights[rel], "pages": max((results[e][rel].pages for e in engines), default=0)})
    return items, {"docs": len(rels), "pages": len(pool)}


# ---------------------------------------------------------------------- imagem da pagina
def render_page(pdf: Path, page: int, max_side: int = 1800) -> bytes:
    with pymupdf.open(str(pdf)) as doc:
        pg = doc[page - 1]
        side = max(pg.rect.width, pg.rect.height) or 1.0
        zoom = min(2.0, max_side / side)
        pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("png")


def page_count(pdf: Path) -> int:
    try:
        with pymupdf.open(str(pdf)) as doc:
            return doc.page_count
    except Exception:  # noqa: BLE001
        return 0


__all__ = ["Doc", "Result", "Scan", "MAX_PER_DOC", "SIDE_ENGINE", "doc_from_rel", "result_base", "storage_path",
           "result_paths", "read_result",
           "load_info", "result_bodies", "original_pages", "ocr_page_numbers", "sample_pages", "render_page",
           "page_count"]
