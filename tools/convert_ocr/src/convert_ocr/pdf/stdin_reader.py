"""Leitura de linhas do stdin que nao bloqueia o processo em ReadFile sincrono.

No Windows, enquanto uma thread do pai esta bloqueada num ReadFile sincrono do pipe de stdin, processos filhos
criados por multiprocessing (spawn) nao terminam de iniciar ate que chegue algum dado nesse pipe. Por isso,
quando o stdin e um pipe no Windows, lemos por polling: PeekNamedPipe informa quantos bytes ha, e so entao
os.read e chamado. Em POSIX (ou stdin de console) a leitura bloqueante normal e usada.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator

POLL_S = 0.02


def _windows_pipe_handle() -> int | None:
    if sys.platform != "win32":
        return None
    try:
        import msvcrt
        import _winapi
        handle = msvcrt.get_osfhandle(0)
        if _winapi.GetFileType(handle) == 3:   # FILE_TYPE_PIPE
            return handle
    except (OSError, ImportError, AttributeError):
        return None
    return None


def iter_lines(stop=None, max_line: int = 1 << 20) -> Iterator[bytes]:
    """Gera linhas (bytes, sem o '\\n') ate EOF ou ate `stop` (threading.Event) ser acionado.
    Linhas maiores que `max_line` sao devolvidas truncadas em pedacos de max_line bytes (quem consome
    decide o que fazer com um pedaco sem '\\n')."""
    handle = _windows_pipe_handle()
    buf = bytearray()

    def flush_lines():
        while True:
            i = buf.find(b"\n")
            if i < 0:
                if len(buf) > max_line:
                    chunk = bytes(buf[:max_line])
                    del buf[:max_line]
                    yield chunk
                return
            line = bytes(buf[:i])
            del buf[:i + 1]
            yield line.rstrip(b"\r")

    while stop is None or not stop.is_set():
        if handle is not None:
            import _winapi
            try:
                avail, _ = _winapi.PeekNamedPipe(handle, 0)
            except OSError:        # pipe fechado pelo outro lado: EOF
                break
            if not avail:
                time.sleep(POLL_S)
                continue
            try:
                data = os.read(0, min(avail, 1 << 16))
            except OSError:
                break
        else:
            try:
                data = os.read(0, 1 << 16)
            except OSError:
                break
        if not data:
            break
        buf.extend(data)
        yield from flush_lines()
    if buf:
        yield bytes(buf)
