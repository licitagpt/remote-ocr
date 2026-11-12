#!/bin/bash
# Faz o build da imagem remote-ocr e recria o container.
# Uso: ./deploy.sh [--no-build]
#   --no-build  pula o build e apenas recria o container com a imagem existente
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="licitagpt/remote-ocr"
BUILD=1

for arg in "$@"; do
  case "$arg" in
    --no-build) BUILD=0 ;;
    *) echo "Argumento desconhecido: $arg"; exit 1 ;;
  esac
done

# ------------------------------------------------------------------ build
if [ "$BUILD" -eq 1 ]; then
  echo "==> Build da imagem $IMAGE (source: $SCRIPT_DIR/src)"
  docker build -t "$IMAGE" "$SCRIPT_DIR/src"
  echo "==> Build concluído"
fi

# ------------------------------------------------------------------ lê a API key do container em execução
API_KEY=""
if docker inspect remote-ocr &>/dev/null; then
  API_KEY=$(docker exec remote-ocr printenv REMOTEOCR_API_KEY 2>/dev/null || true)
fi

if [ -z "$API_KEY" ]; then
  if [ -n "${REMOTEOCR_API_KEY:-}" ]; then
    API_KEY="$REMOTEOCR_API_KEY"
    echo "Usando REMOTEOCR_API_KEY do ambiente."
  else
    echo "ERRO: não foi possível ler REMOTEOCR_API_KEY do container remote-ocr (está em execução?)"
    echo "Passe a chave via variável de ambiente: REMOTEOCR_API_KEY=<chave> $0"
    exit 1
  fi
fi

# ------------------------------------------------------------------ recria o container
echo "==> Parando e removendo container remote-ocr..."
docker stop remote-ocr 2>/dev/null || true
docker rm   remote-ocr 2>/dev/null || true

echo "==> Criando novo container remote-ocr..."
docker run -d \
  --name remote-ocr \
  --restart unless-stopped \
  --gpus all \
  -v /docker/docker-data/volumes/remote-ocr-data/_data:/data:z \
  -e REMOTEOCR_HOST=10.0.0.1:43210 \
  -e REMOTEOCR_API_KEY="$API_KEY" \
  -e REMOTEOCR_INSTANCES=6 \
  -e REMOTEOCR_DEVICE=cuda \
  -e REMOTEOCR_ENGINE=auto \
  -e REMOTEOCR_STORAGE=/data \
  -e REMOTEOCR_SHUTDOWN_GRACE=90 \
  -e TZ=America/Sao_Paulo \
  "$IMAGE"

echo ""
echo "Container remote-ocr recriado com a nova imagem."
echo "Acompanhe os logs:"
echo "  docker logs -f remote-ocr"
