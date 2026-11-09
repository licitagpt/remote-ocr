"""Engine de execucao: inicializa o processo uma vez e trata um documento por vez (mesmo desenho do convert_pdf).

O paralelismo e de quem inicia o convert_ocr (o Workers .NET abre N processos `convert_ocr server`). Excecao
durante o OCR -> resposta `error` tipada (document.process nunca levanta); demora -> timeout cooperativo entre
paginas; travamento numa chamada nativa -> watchdog do server-mode encerra o processo.
"""

from __future__ import annotations

import logging
import os
import time
import traceback
from dataclasses import dataclass
from typing import Any

from convert_ocr.config import Settings

log = logging.getLogger("convert_ocr.engine")


@dataclass(frozen=True)
class Task:
    id: str
    pdf: str
    md: str | None = None        # .md de entrada (default: ao lado do PDF)
    out: str | None = None       # .md de saida (default: substitui o de entrada)
    engine: str | None = None    # motor por requisicao (default: settings.engine)


def error_result(task: Task, code: str, message: str) -> dict[str, Any]:
    return {"pdf": task.pdf, "md": task.md or "", "out": task.out or task.md or "", "ok": False, "status": "error",
            "error": {"code": code, "message": message}}


class Engine:
    def __init__(self, settings: Settings, mode: str = "local"):
        self.settings = settings
        self.mode = mode
        self.output = settings.output_for(mode)      # replace | ocr-md (quando a tarefa nao informa `out`)
        from convert_ocr.core.document import init_process
        init_process(settings)
        self.started_at = time.time()
        self.n_done = self.n_errors = self.n_timeouts = 0
        self.ms_total = 0.0
        self.pages = 0

    def warm_up(self) -> dict[str, Any]:
        """Carrega o motor default (modelos) antes da 1a requisicao; devolve a descricao dele."""
        from convert_ocr.core.engines import get_engine
        try:
            return get_engine(self.settings.engine, self.settings).info()
        except Exception as e:  # noqa: BLE001
            log.warning("motor %s indisponivel na subida: %s: %s", self.settings.engine, type(e).__name__, e)
            return {"engine": self.settings.engine, "error": f"{type(e).__name__}: {e}"}

    def run(self, task: Task, progress=None) -> dict[str, Any]:
        from convert_ocr.core.document import process
        try:
            result = process(task.pdf, task.md, task.out, self.settings, engine=task.engine, progress=progress,
                             output=self.output).to_dict()
        except BaseException as e:  # noqa: BLE001  (ultima barreira)
            result = error_result(task, "INTERNAL", f"{type(e).__name__}: {e}")
            result["trace"] = traceback.format_exc()[-2000:]
        self.n_done += 1
        if not result.get("ok"):
            self.n_errors += 1
            if (result.get("error") or {}).get("code") == "TIMEOUT":
                self.n_timeouts += 1
        else:
            self.pages += int(result.get("ocr_done", 0) or 0)
            self.ms_total += float((result.get("ms") or {}).get("total", 0) or 0)
        return result

    def status(self) -> dict[str, Any]:
        return dict(done=self.n_done, errors=self.n_errors, timeouts=self.n_timeouts, ocr_pages=self.pages,
                    cpu_ms=int(self.ms_total), uptime_s=round(time.time() - self.started_at, 1), pid=os.getpid())

    def close(self) -> None:
        from convert_ocr.core.engines import close_all
        close_all()
