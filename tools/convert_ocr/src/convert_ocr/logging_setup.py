"""Logs: console colorido em stderr + arquivo rotativo em LOG_DIR (mesmo esquema do convert_pdf).

stdout nunca e usado pelo logging: no server-mode ele e exclusivo do protocolo NDJSON. As bibliotecas de OCR
(rapidocr, pymupdf4llm) escrevem avisos com print(); o server-mode redireciona sys.stdout para stderr antes de
carrega-las (ver runtime/server.py). Cada processo grava no seu proprio arquivo (sufixo com o PID).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from convert_ocr.config import Settings

LOGGER_NAME = "convert_ocr"

RESET = "\x1b[0m"
COLORS = {
    "grey": "\x1b[90m", "red": "\x1b[31m", "green": "\x1b[32m", "yellow": "\x1b[33m",
    "blue": "\x1b[34m", "magenta": "\x1b[35m", "cyan": "\x1b[36m", "bold": "\x1b[1m",
}
LEVEL_COLORS = {
    logging.DEBUG: "grey", logging.INFO: "", logging.WARNING: "yellow",
    logging.ERROR: "red", logging.CRITICAL: "red",
}
STATUS_COLORS = {"ok": "green", "no_ocr": "grey", "skipped": "grey", "error": "red", "TIMEOUT": "red"}

_color_enabled = False


def color_enabled() -> bool:
    return _color_enabled


def paint(text: str, name: str) -> str:
    if not _color_enabled or not name:
        return text
    return f"{COLORS.get(name, '')}{text}{RESET}"


def paint_status(status: str) -> str:
    return paint(status, STATUS_COLORS.get(status, ""))


def fmt_ms(ms: float) -> str:
    return f"{ms / 1000:.2f}s" if ms >= 1000 else f"{ms:.0f}ms"


class ColorFormatter(logging.Formatter):
    def __init__(self, use_color: bool):
        super().__init__("%(asctime)s %(levelname).1s %(message)s", datefmt="%H:%M:%S")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        s = super().format(record)
        if not self.use_color:
            return s
        c = LEVEL_COLORS.get(record.levelno, "")
        if c and record.levelno != logging.INFO:
            s = f"{COLORS[c]}{s}{RESET}"
        return s


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def _want_color(settings: Settings) -> bool:
    if settings.no_color or os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stderr.isatty()
    except Exception:  # noqa: BLE001
        return False


def quiet_libraries() -> None:
    """Silencia os loggers proprios das bibliotecas de OCR (o RapidOCR instala um handler colorido no stderr)."""
    for name in ("RapidOCR", "rapidocr", "onnxruntime"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = False
        lg.setLevel(logging.WARNING)
    if "onnxruntime" in sys.modules:          # logger nativo (C++) do onnxruntime: so erros
        from convert_ocr.core.device import quiet_onnxruntime
        quiet_onnxruntime()


def setup_logging(settings: Settings, file_logging: bool = True) -> list[logging.Handler]:
    """Configura o logging do processo. Retorna os handlers instalados."""
    global _color_enabled
    _force_utf8()
    _color_enabled = _want_color(settings)
    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(settings.log_level.upper())
    root.propagate = False
    for h in list(root.handlers):
        root.removeHandler(h)
    handlers: list[logging.Handler] = []
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(ColorFormatter(_color_enabled))
    handlers.append(console)
    if file_logging:
        try:
            log_dir = settings.effective_log_dir
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_dir / f"convert_ocr-{os.getpid()}.log", maxBytes=settings.log_file_max_mb * 1024 * 1024,
                backupCount=settings.log_file_backups, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s pid=%(process)d %(name)s %(message)s"))
            handlers.append(fh)
        except OSError as e:
            root.addHandler(console)
            root.warning("nao foi possivel criar o log em %s (%s); logando so no console", settings.effective_log_dir, e)
            root.removeHandler(console)
    for h in handlers:
        root.addHandler(h)
    # o convert_pdf (cabecalho/rodape, markdown) loga no logger dele: leva junto para os mesmos handlers
    sibling = logging.getLogger("convert_pdf")
    if not sibling.handlers:
        sibling.setLevel(settings.log_level.upper())
        sibling.propagate = False
        for h in handlers:
            sibling.addHandler(h)
    quiet_libraries()
    return handlers


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)
