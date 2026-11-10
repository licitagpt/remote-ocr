# remote-ocr: cliente de OCR do licitagpt (.NET + convert_ocr em Python), para rodar em máquinas com GPU NVIDIA.
#
#   docker build -t licitagpt/remote-ocr .                       # CUDA (onnxruntime-gpu + bibliotecas CUDA/cuDNN via pip)
#   docker build -t licitagpt/remote-ocr --build-arg DEVICE=cpu . # só CPU (imagem menor)
#   docker run -d --name remote-ocr --gpus all --restart unless-stopped --stop-timeout 120 \
#     -e REMOTEOCR_HOST=186.216.247.55:28747 -e REMOTEOCR_API_KEY=MINHA_SENHA_SECRETA \
#     -e REMOTEOCR_INSTANCES=4 -e REMOTEOCR_DEVICE=cuda \
#     -v remote-ocr-data:/data licitagpt/remote-ocr
#
# A GPU entra pelo NVIDIA Container Toolkit (--gpus all), que injeta o driver (libcuda.so.1); o runtime CUDA, cuBLAS e
# cuDNN vêm dos pacotes pip nvidia-* instalados no venv (scripts/setup_gpu.py --cuda), e o .NET os põe no LD_LIBRARY_PATH
# dos processos Python. --stop-timeout dá tempo de o cliente terminar os documentos em voo (REMOTEOCR_SHUTDOWN_GRACE).

# ---------- build (.NET) ----------
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src
COPY src/RemoteOcr.csproj src/
RUN dotnet restore src/RemoteOcr.csproj
COPY src/ src/
RUN dotnet publish src/RemoteOcr.csproj -c Release -o /app/publish --no-restore

# ---------- runtime (.NET + Python) ----------
FROM mcr.microsoft.com/dotnet/runtime:10.0
ARG DEVICE=cuda

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV TZ=America/Sao_Paulo
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
RUN python3 -m venv $VIRTUAL_ENV && pip install --no-cache-dir --upgrade pip

# A ferramenta (autocontida: não depende do convert_pdf) com o motor de layout; GPU conforme DEVICE.
COPY tools/convert_ocr/ /opt/tools/convert_ocr/
RUN pip install --no-cache-dir -e "/opt/tools/convert_ocr[layout]"
# onnxruntime-gpu + CUDA/cuDNN/cuRAND via pip. A verificação final do script falha aqui porque o build não tem GPU
# (a sessão CUDA só é testada na subida do container); o que importa é a instalação ter terminado.
RUN if [ "$DEVICE" = "cuda" ]; then \
      python /opt/tools/convert_ocr/scripts/setup_gpu.py --cuda || echo "setup_gpu: sem GPU no build; providers conferidos na subida"; \
      python -c "import onnxruntime as ort; assert 'CUDAExecutionProvider' in ort.get_available_providers(), ort.get_available_providers(); print('onnxruntime-gpu', ort.__version__)"; \
    fi
RUN python /opt/tools/convert_ocr/scripts/prefetch_models.py

# Configuração padrão (sobrescreva no docker run): host e chave são obrigatórios.
ENV REMOTEOCR_PYTHON=/opt/venv/bin/python \
    REMOTEOCR_TOOL_DIR=/opt/tools/convert_ocr \
    REMOTEOCR_STORAGE=/data \
    REMOTEOCR_INSTANCES=4 \
    REMOTEOCR_DEVICE=${DEVICE} \
    REMOTEOCR_ENGINE=auto \
    REMOTEOCR_SHUTDOWN_GRACE=90

VOLUME /data
WORKDIR /app
COPY --from=build /app/publish .

ENTRYPOINT ["dotnet", "RemoteOcr.dll"]
