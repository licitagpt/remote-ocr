"""Troca o onnxruntime do ambiente atual pela variante com GPU (ou volta para CPU) e verifica os providers.

    python scripts/setup_gpu.py --cuda    # NVIDIA: onnxruntime-gpu + CUDA/cuDNN via pip (nvidia-*); Windows e Linux
    python scripts/setup_gpu.py --dml     # qualquer GPU DirectX 12: onnxruntime-directml; so Windows; so o RapidOCR
    python scripts/setup_gpu.py --cpu     # volta ao onnxruntime de CPU
    python scripts/setup_gpu.py --check   # so mostra o que esta instalado e os providers

As tres variantes instalam o MESMO modulo `onnxruntime`, por isso o pip nao resolve a troca sozinho: este script
desinstala as que houver e instala a pedida. Para o CUDA, le em `onnxruntime.capi.build_and_package_info` a versao
do CUDA para a qual o onnxruntime-gpu foi compilado e instala os pacotes `nvidia-*` correspondentes (CUDA 13:
nvidia-cuda-runtime, nvidia-cublas, nvidia-cufft, nvidia-cudnn-cu13; CUDA 12: os mesmos com sufixo -cu12).
O driver NVIDIA precisa suportar essa versao do CUDA (nvidia-smi mostra a maxima suportada).
"""

from __future__ import annotations

import argparse
import importlib
import subprocess
import sys

ORT_PACKAGES = ("onnxruntime", "onnxruntime-gpu", "onnxruntime-directml")


def pip(*args: str) -> int:
    cmd = [sys.executable, "-m", "pip", *args]
    print("$", " ".join(cmd), flush=True)
    return subprocess.call(cmd)


def installed() -> dict[str, str]:
    out = subprocess.run([sys.executable, "-m", "pip", "list", "--format=freeze"], capture_output=True, text=True).stdout
    found = {}
    for line in out.splitlines():
        name, _, ver = line.partition("==")
        if name.lower() in ORT_PACKAGES or name.lower().startswith("nvidia-"):
            found[name.lower()] = ver
    return found


def cuda_packages(cuda_version: str) -> list[str]:
    major = int(cuda_version.split(".")[0])
    if major >= 13:
        return [f"nvidia-cuda-runtime>={major},<{major + 1}", f"nvidia-cublas>={major},<{major + 1}", "nvidia-cufft",
                f"nvidia-cudnn-cu{major}>=9"]
    return [f"nvidia-cuda-runtime-cu{major}", f"nvidia-cublas-cu{major}", f"nvidia-cufft-cu{major}",
            f"nvidia-cudnn-cu{major}>=9"]


def check() -> int:
    found = installed()
    print("pacotes:", ", ".join(f"{k} {v}" for k, v in sorted(found.items())) or "nenhum onnxruntime instalado")
    try:
        importlib.invalidate_caches()
        ort = importlib.import_module("onnxruntime")
    except ImportError as e:
        print("import onnxruntime falhou:", e)
        return 1
    try:
        from onnxruntime.capi import build_and_package_info as bi
        print(f"onnxruntime: {bi.package_name} {bi.__version__}" + (f" (CUDA {bi.cuda_version})" if getattr(bi, "cuda_version", None) else ""))
    except Exception:  # noqa: BLE001
        print("onnxruntime:", ort.__version__)
    if hasattr(ort, "preload_dlls"):
        try:
            ort.preload_dlls()
        except Exception as e:  # noqa: BLE001
            print("preload_dlls:", e)
    provs = ort.get_available_providers()
    print("providers:", provs, "| get_device():", ort.get_device())
    gpu = [p for p in provs if p in ("CUDAExecutionProvider", "DmlExecutionProvider")]
    if not gpu:
        print("sem provider de GPU: o convert_ocr roda em CPU")
        return 0
    # um provider listado ainda pode falhar ao criar a sessao (DLL do CUDA/cuDNN ausente): testa com o RapidOCR
    try:
        from convert_ocr.config import Settings
        from convert_ocr.core import device as dev
        from convert_ocr.core.engines.base import RapidRecognizer
        s = Settings()
        RapidRecognizer.get(s)
        d = dev.probe(s)
        print("RapidOCR carregado; provider em uso:", d.active, "" if d.gpu else f"(fallback: {d.note})")
        return 0 if d.gpu else 1
    except ImportError:
        print("GPU disponivel para o convert_ocr:", ", ".join(gpu), "(convert_ocr nao instalado; sessao nao testada)")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--cuda", action="store_true", help="onnxruntime-gpu + bibliotecas CUDA/cuDNN via pip")
    g.add_argument("--dml", action="store_true", help="onnxruntime-directml (Windows)")
    g.add_argument("--cpu", action="store_true", help="onnxruntime de CPU")
    g.add_argument("--check", action="store_true", help="so verificar")
    ap.add_argument("--ort-version", default="", help="versao do onnxruntime a instalar (default: a mais recente)")
    a = ap.parse_args()
    if a.check:
        return check()
    if a.dml and sys.platform != "win32":
        print("DirectML so existe no Windows; use --cuda")
        return 2
    present = [p for p in ORT_PACKAGES if p in installed()]
    if present:
        pip("uninstall", "-y", *present)
    target = "onnxruntime-gpu" if a.cuda else "onnxruntime-directml" if a.dml else "onnxruntime"
    spec = f"{target}=={a.ort_version}" if a.ort_version else target
    if pip("install", spec) != 0:
        return 1
    if a.cuda:
        importlib.invalidate_caches()
        out = subprocess.run([sys.executable, "-c", "from onnxruntime.capi import build_and_package_info as b; print(b.cuda_version)"],
                             capture_output=True, text=True).stdout.strip()
        if not out or out == "None":
            print("nao foi possivel ler a versao do CUDA do onnxruntime-gpu; instale as bibliotecas CUDA manualmente")
        else:
            print("onnxruntime-gpu compilado para CUDA", out)
            if pip("install", *cuda_packages(out)) != 0:
                return 1
    return subprocess.call([sys.executable, __file__, "--check"])


if __name__ == "__main__":
    sys.exit(main())
