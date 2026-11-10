"""Memoria residente (RSS) de um processo, para o limite `max_rss_mb` dos server-modes e do sidecar de layout.

Bibliotecas nativas (MuPDF, onnxruntime, `pymupdf.features` do modelo de layout) podem crescer sem limite ao
longo de milhares de documentos sem que nada apareca no heap Python; o unico sinal confiavel e o RSS do processo.
Sem `psutil`: /proc no Linux, GetProcessMemoryInfo no Windows, getrusage (pico) no macOS. 0.0 = nao mediu.
"""

from __future__ import annotations

import os
import sys

_MB = float(1 << 20)


def rss_mb(pid: int | None = None) -> float:
    """RSS em MB do processo `pid` (default: o atual); 0.0 se nao for possivel medir nesta plataforma."""
    try:
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid or 'self'}/statm", encoding="ascii") as f:
                return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / _MB
        if sys.platform == "win32":
            return _rss_windows(pid)
        if pid is None:
            import resource
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return peak / _MB if sys.platform == "darwin" else peak / 1024.0     # macOS em bytes, BSD em KB
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _rss_windows(pid: int | None) -> float:
    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    kernel32 = ctypes.windll.kernel32          # type: ignore[attr-defined]
    psapi = ctypes.windll.psapi                # type: ignore[attr-defined]
    own = pid is None
    handle = kernel32.GetCurrentProcess() if own else kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
    if not handle:
        return 0.0
    try:
        pmc = ProcessMemoryCounters()
        pmc.cb = ctypes.sizeof(pmc)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
            return 0.0
        return pmc.WorkingSetSize / _MB
    finally:
        if not own:
            kernel32.CloseHandle(handle)
