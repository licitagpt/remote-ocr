"""server-mode: protocolo NDJSON por stdin/stdout, um documento por vez (mesmo protocolo do convert_pdf).

Entrada (uma linha JSON): {"id":"…","cmd":"convert","pdf":"files/pncp/…/x.pdf"} e, opcionais, "md" (o .md do
convert_pdf; default ao lado do PDF), "out" (gravar o resultado em outro arquivo; sem ele vale OUTPUT, que no
server-mode e `replace` por default: substitui o .md) e "engine" (auto | rapidocr | pymupdf4llm). `cmd` tambem aceita
"ocr" como sinonimo. Mais `ping`, `status`, `shutdown`.
Saida: {"event":"ready",…} na subida e, por documento, o OcrResult (`ok`, `status`, `ocr_pages`, `ocr_done`,
`empty_pages`, `hf_removed`, `token_count`, `md_size_bytes`/`tamanhoMd`, `ms`, …).

stdout e exclusivo do protocolo: `sys.stdout` e redirecionado para stderr logo na subida, porque as bibliotecas
de OCR escrevem avisos com print(). Watchdog: se uma chamada nativa travar alem de `timeout + WATCHDOG_GRACE_S`,
responde TIMEOUT, emite {"event":"exit","reason":"watchdog"} e encerra com codigo 3; o .NET reinicia o processo.

Limite de memoria (`--max-rss-mb`): depois de responder um documento, se o RSS do processo passou do limite, emite
{"event":"exit","reason":"memory","retry":true,...} e encerra com codigo 4 (EXIT_MEMORY). Um pedido que chegue
nesse intervalo fica SEM resposta de proposito: `retry` diz ao .NET que pode reenvia-lo ao processo novo (nenhum
documento foi processado pela metade). Protege contra crescimento das bibliotecas nativas, invisivel ao heap Python.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
import uuid
from collections import deque
from typing import Any

import pymupdf

from convert_ocr import __version__
from convert_ocr.config import ENGINES, Settings
from convert_ocr.logging_setup import fmt_ms, paint, paint_status
from convert_ocr.runtime.engine import Engine, Task, error_result
from convert_pdf.runtime.procmem import rss_mb
from convert_pdf.runtime.stdin_reader import iter_lines

log = logging.getLogger("convert_ocr.server")
MAX_LINE = 1 << 20
WATCHDOG_GRACE_S = 30.0
EXIT_WATCHDOG = 3
EXIT_MEMORY = 4


class Server:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._out_lock = threading.Lock()
        self._stdout = sys.stdout.buffer
        sys.stdout = sys.stderr                 # prints perdidos das bibliotecas nao corrompem o protocolo
        self._stop = threading.Event()
        self._queue: deque[Task] = deque()
        self._qlock = threading.Lock()
        self._wake = threading.Event()
        self._inflight: Task | None = None
        self._inflight_since = 0.0
        self._n = 0
        self._signal: int | None = None                 # sinal (SIGTERM/SIGINT) que pediu o encerramento
        self._recycle = False                          # encerrando por memoria: pedidos na fila ficam sem resposta
        self.engine = Engine(settings, "server")       # default: substitui o .md (OUTPUT=auto -> replace)

    # ------------------------------------------------------------------ saida
    def emit(self, obj: dict[str, Any]) -> None:
        data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._out_lock:
            try:
                self._stdout.write(data)
                self._stdout.flush()
            except (BrokenPipeError, OSError):
                self._stop.set()
                self._wake.set()

    def _reply_error(self, req_id: str | None, code: str, message: str) -> None:
        msg: dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
        if req_id is not None:
            msg["id"] = req_id
        self.emit(msg)

    # ------------------------------------------------------------------ entrada
    def _reader(self) -> None:
        for raw in iter_lines(self._stop, MAX_LINE):
            if len(raw) >= MAX_LINE:
                self._reply_error(None, "INVALID_REQUEST", f"linha maior que {MAX_LINE} bytes")
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError as e:
                self._reply_error(None, "INVALID_REQUEST", f"JSON invalido: {e}")
                continue
            if not isinstance(req, dict):
                self._reply_error(None, "INVALID_REQUEST", "esperado um objeto JSON por linha")
                continue
            self._handle(req)
        log.info("stdin fechado; encerrando")
        self._stop.set()
        self._wake.set()

    def status(self) -> dict[str, Any]:
        with self._qlock:
            queued = len(self._queue)
            inflight = self._inflight
            since = self._inflight_since
        st = self.engine.status()
        st.update(queued=queued, inflight=1 if inflight else 0,
                  inflight_pdf=inflight.pdf if inflight else None,
                  inflight_s=round(time.time() - since, 1) if inflight else 0.0,
                  rss_mb=round(rss_mb()), max_rss_mb=self.settings.max_rss_mb)
        return st

    def _handle(self, req: dict[str, Any]) -> None:
        req_id = req.get("id")
        if req_id is not None and not isinstance(req_id, str):
            req_id = str(req_id)
        cmd = str(req.get("cmd", "convert")).lower()
        if cmd == "ping":
            self.emit({"id": req_id, "ok": True, "pong": True, "ts": time.time()})
        elif cmd == "status":
            self.emit({"id": req_id, "ok": True, **self.status()})
        elif cmd == "shutdown":
            self.emit({"id": req_id, "ok": True, "shutdown": True})
            self._stop.set()
            self._wake.set()
        elif cmd in ("convert", "ocr"):
            pdf = req.get("pdf")
            if not pdf or not isinstance(pdf, str):
                self._reply_error(req_id, "INVALID_REQUEST", "campo 'pdf' obrigatorio")
                return
            if req_id is None:
                req_id = uuid.uuid4().hex[:12]
            md, out, engine = req.get("md"), req.get("out"), req.get("engine")
            if engine is not None and (not isinstance(engine, str) or engine.lower() not in ENGINES):
                self._reply_error(req_id, "INVALID_REQUEST", f"campo 'engine' deve ser {'|'.join(ENGINES)}")
                return
            task = Task(id=req_id, pdf=str(self.settings.resolve_path(pdf)),
                        md=str(self.settings.resolve_path(md)) if isinstance(md, str) and md else None,
                        out=str(self.settings.resolve_path(out)) if isinstance(out, str) and out else None,
                        engine=engine.lower() if isinstance(engine, str) else None)
            if self._stop.is_set():
                if not self._recycle:              # reciclagem por memoria: fica sem resposta e o .NET reenvia
                    self._emit_result(task, error_result(task, "SHUTDOWN", "servidor encerrando"))
                return
            with self._qlock:
                self._queue.append(task)
            self._wake.set()
        else:
            self._reply_error(req_id, "INVALID_REQUEST", f"comando desconhecido: {cmd!r}")

    # ------------------------------------------------------------------ watchdog
    def _watchdog(self) -> None:
        limit = self.settings.timeout_s + WATCHDOG_GRACE_S
        while not self._stop.wait(0.5):
            with self._qlock:
                task, since = self._inflight, self._inflight_since
            if task is not None and time.time() - since > limit:
                log.error("WATCHDOG: %s travado ha %.0fs (timeout %.0fs); encerrando o processo",
                          os.path.basename(task.pdf), time.time() - since, self.settings.timeout_s)
                self._emit_result(task, error_result(task, "TIMEOUT",
                                                     f"OCR travado alem de {limit:.0f}s; processo encerrado pelo watchdog"))
                self.emit({"event": "exit", "reason": "watchdog", "pid": os.getpid()})
                for h in logging.getLogger("convert_ocr").handlers:
                    try:
                        h.flush()
                    except Exception:  # noqa: BLE001
                        pass
                os._exit(EXIT_WATCHDOG)

    def _install_signal_handlers(self) -> None:
        """SIGTERM, SIGINT (Ctrl+C) e SIGBREAK (Windows) pedem o encerramento ordenado: o documento em voo termina e
        e respondido normalmente, a fila recebe SHUTDOWN e o processo sai com 0. Sem isto o Ctrl+C no terminal do
        Workers (que chega ao grupo de processos inteiro) virava KeyboardInterrupt dentro da conversao, e o
        documento era respondido como erro INTERNAL em vez de ser reexecutado depois. Quem encerra de verdade e o
        .NET (comando `shutdown`, EOF no stdin ou kill)."""
        def handler(signum, _frame):
            self._signal = signum
            self._stop.set()
            self._wake.set()
        for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError, AttributeError):
                pass

    # ------------------------------------------------------------------ loop
    def run(self) -> int:
        s = self.settings
        info = self.engine.warm_up()
        device = info.get("device") or {}
        self.emit({"event": "ready", "version": __version__, "pid": os.getpid(), "engine": s.engine,
                   "backend": info.get("backend"), "engine_error": info.get("error"), "device": device or None,
                   "output": self.engine.output,
                   "dpi": s.ocr_dpi, "timeout_s": s.timeout_s, "max_rss_mb": s.max_rss_mb, "pymupdf": pymupdf.version[0],
                   "storage": s.storage_dir, "tiktoken": s.tiktoken_encoding or None})
        log.info("server-mode pronto (pid %d): engine=%s%s, dispositivo=%s, dpi=%d, timeout=%.0fs, storage=%s, log=%s",
                 os.getpid(), s.engine, f" ({info.get('backend')})" if info.get("backend") else "",
                 device.get("active") or device.get("provider") or "?", s.ocr_dpi, s.timeout_s, s.storage_dir,
                 s.effective_log_dir)
        threading.Thread(target=self._reader, name="stdin-reader", daemon=True).start()
        threading.Thread(target=self._watchdog, name="watchdog", daemon=True).start()
        self._install_signal_handlers()
        try:
            while not self._stop.is_set():
                task = self._next_task()
                if task is None:
                    self._wake.wait(0.25)
                    self._wake.clear()
                    continue
                self._run_one(task)
        except KeyboardInterrupt:
            pass
        finally:
            with self._qlock:
                rest = list(self._queue)
                self._queue.clear()
            if not self._recycle:                      # reciclagem: o .NET reenvia o que ficou sem resposta
                for t in rest:
                    self._emit_result(t, error_result(t, "SHUTDOWN", "servidor encerrando"))
            self.engine.close()
            log.info("server-mode encerrado apos %d documentos%s%s", self._n,
                     f" (reinicio por memoria; {len(rest)} pedido(s) devolvido(s) ao Workers)" if self._recycle else "",
                     f" (sinal {self._signal})" if self._signal is not None else "")
        return EXIT_MEMORY if self._recycle else 0

    def _next_task(self) -> Task | None:
        with self._qlock:
            if not self._queue:
                return None
            task = self._queue.popleft()
            self._inflight, self._inflight_since = task, time.time()
            return task

    def _run_one(self, task: Task) -> None:
        try:
            result = self.engine.run(task)
        finally:
            with self._qlock:
                self._inflight = None
        self._emit_result(task, result)
        self._check_memory()

    def _check_memory(self) -> None:
        """Entre documentos: RSS acima de `max_rss_mb` -> avisa (evento exit com retry) e encerra com EXIT_MEMORY.
        O reader para de ler o stdin; o que ja estava na fila nao e respondido (o .NET reenvia)."""
        limit = self.settings.max_rss_mb
        if limit <= 0 or self._recycle:
            return
        rss = rss_mb()
        if rss <= limit:
            return
        log.warning("memoria residente %.0f MB acima do limite de %d MB apos %d documentos; encerrando para reinicio",
                    rss, limit, self._n)
        self._recycle = True
        self.emit({"event": "exit", "reason": "memory", "retry": True, "rss_mb": round(rss), "limit_mb": limit,
                   "documents": self._n, "pid": os.getpid()})
        self._stop.set()
        self._wake.set()

    def _emit_result(self, task: Task, result: dict[str, Any]) -> None:
        self._n += 1
        result = dict(result)
        result["id"] = task.id
        self.emit(result)
        name = os.path.basename(task.pdf)
        if result.get("ok"):
            ms = (result.get("ms") or {}).get("total", 0)
            log.info("[%6d] %-7s %8s ocr=%-3d/%-4dp vazias=%-2d hf=%-4d tok=%-7d %s %s", self._n,
                     paint_status(result.get("status", "")), paint(fmt_ms(ms), "cyan"), result.get("ocr_done", 0),
                     result.get("pages", 0), result.get("empty_pages", 0), result.get("hf_removed", 0),
                     result.get("token_count", 0), result.get("engine", ""), name)
        else:
            err = result.get("error", {})
            log.error("[%6d] %-7s %s %s: %s", self._n, paint_status("error"), name, err.get("code"), err.get("message"))


def run_server(settings: Settings) -> int:
    return Server(settings).run()
