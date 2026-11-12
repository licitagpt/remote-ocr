#!/bin/bash
# Faz o build da imagem remote-ocr e recria o container.
#
# Se o container "remote-ocr" já existe, copia todas as variáveis de ambiente
# dele e recria com a nova imagem. Caso contrário, --host, --api-key,
# --instances e --device são obrigatórios.
#
# Uso:
#   ./deploy.sh [opções]
#
# Opções obrigatórias (apenas quando o container não existe):
#   --host <host:porta>     Endereço do Workers   (ex: 10.0.0.1:43210)
#   --api-key <chave>       Chave de API do Workers
#   --instances <n>         Número de instâncias Python (ex: 6)
#   --device <device>       Dispositivo OCR       (ex: cuda, cpu)
#
# Opções opcionais:
#   --engine <engine>       Motor OCR             (padrão: auto)
#   --storage <caminho>     Pasta de armazenamento (padrão: /data)
#   --shutdown-grace <s>    Segundos de graceful shutdown (padrão: 90)
#   --name <nome>           Nome do container     (padrão: remote-ocr)
#   --image <imagem>        Nome da imagem Docker (padrão: licitagpt/remote-ocr)
#   --volume <src:dst[:z]>  Volume extra (pode repetir)
#   --no-build              Pula o build; usa a imagem existente
#   --help                  Exibe esta ajuda

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ------------------------------------------------------------------ defaults
CONTAINER_NAME="remote-ocr"
IMAGE="licitagpt/remote-ocr"
BUILD=1

# parâmetros que podem vir do container existente ou da linha de comando
OPT_HOST=""
OPT_API_KEY=""
OPT_INSTANCES=""
OPT_DEVICE=""
OPT_ENGINE=""
OPT_STORAGE=""
OPT_SHUTDOWN_GRACE=""
EXTRA_ENVS=()   # --e KEY=VALUE extras vindos da linha de comando
EXTRA_VOLS=()   # --volume extras vindos da linha de comando

# ------------------------------------------------------------------ parse args
print_help() {
  grep '^#' "$0" | grep -v '^#!/' | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)        print_help ;;
    --no-build)       BUILD=0; shift ;;
    --name)           CONTAINER_NAME="$2"; shift 2 ;;
    --image)          IMAGE="$2"; shift 2 ;;
    --host)           OPT_HOST="$2"; shift 2 ;;
    --api-key)        OPT_API_KEY="$2"; shift 2 ;;
    --instances)      OPT_INSTANCES="$2"; shift 2 ;;
    --device)         OPT_DEVICE="$2"; shift 2 ;;
    --engine)         OPT_ENGINE="$2"; shift 2 ;;
    --storage)        OPT_STORAGE="$2"; shift 2 ;;
    --shutdown-grace) OPT_SHUTDOWN_GRACE="$2"; shift 2 ;;
    --volume|-v)      EXTRA_VOLS+=("$2"); shift 2 ;;
    --env|-e)         EXTRA_ENVS+=("$2"); shift 2 ;;
    *) echo "Argumento desconhecido: $1"; echo "Use --help para ver as opções."; exit 1 ;;
  esac
done

# ------------------------------------------------------------------ build
if [ "$BUILD" -eq 1 ]; then
  echo "==> Build da imagem $IMAGE (contexto: $SCRIPT_DIR)"
  docker build -t "$IMAGE" -f "$SCRIPT_DIR/Dockerfile" "$SCRIPT_DIR"
  echo "==> Build concluído"
fi

# ------------------------------------------------------------------ coleta env vars
# Se o container existe, copia todas as variáveis de ambiente dele como base.
CONTAINER_ENVS=()
CONTAINER_VOLS=()
if docker inspect "$CONTAINER_NAME" &>/dev/null; then
  echo "==> Container '$CONTAINER_NAME' encontrado; copiando configuração..."

  # lê todas as envs do container em execução
  while IFS= read -r line; do
    CONTAINER_ENVS+=("$line")
  done < <(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER_NAME")

  # lê volumes/binds do container
  while IFS= read -r line; do
    [ -n "$line" ] && CONTAINER_VOLS+=("$line")
  done < <(docker inspect --format '{{range .HostConfig.Binds}}{{println .}}{{end}}' "$CONTAINER_NAME")
else
  # container não existe: valida obrigatórios
  MISSING=()
  [ -z "$OPT_HOST" ]      && MISSING+=("--host")
  [ -z "$OPT_API_KEY" ]   && MISSING+=("--api-key")
  [ -z "$OPT_INSTANCES" ] && MISSING+=("--instances")
  [ -z "$OPT_DEVICE" ]    && MISSING+=("--device")
  if [ ${#MISSING[@]} -gt 0 ]; then
    echo "ERRO: container '$CONTAINER_NAME' não encontrado e parâmetros obrigatórios faltando: ${MISSING[*]}"
    echo "Use --help para ver as opções."
    exit 1
  fi
  # monta envs base a partir dos parâmetros obrigatórios
  CONTAINER_ENVS+=(
    "REMOTEOCR_HOST=$OPT_HOST"
    "REMOTEOCR_API_KEY=$OPT_API_KEY"
    "REMOTEOCR_INSTANCES=$OPT_INSTANCES"
    "REMOTEOCR_DEVICE=$OPT_DEVICE"
    "REMOTEOCR_ENGINE=${OPT_ENGINE:-auto}"
    "REMOTEOCR_STORAGE=${OPT_STORAGE:-/data}"
    "REMOTEOCR_SHUTDOWN_GRACE=${OPT_SHUTDOWN_GRACE:-90}"
    "TZ=America/Sao_Paulo"
  )
  CONTAINER_VOLS+=("/docker/docker-data/volumes/remote-ocr-data/_data:/data:z")
fi

# sobrescreve com quaisquer opcionais passados explicitamente na linha de comando
_override_env() {
  local key="$1" val="$2"
  local found=0
  for i in "${!CONTAINER_ENVS[@]}"; do
    if [[ "${CONTAINER_ENVS[$i]}" == "${key}="* ]]; then
      CONTAINER_ENVS[$i]="${key}=${val}"
      found=1
      break
    fi
  done
  [ "$found" -eq 0 ] && CONTAINER_ENVS+=("${key}=${val}")
}

[ -n "$OPT_HOST" ]           && _override_env REMOTEOCR_HOST           "$OPT_HOST"
[ -n "$OPT_API_KEY" ]        && _override_env REMOTEOCR_API_KEY        "$OPT_API_KEY"
[ -n "$OPT_INSTANCES" ]      && _override_env REMOTEOCR_INSTANCES      "$OPT_INSTANCES"
[ -n "$OPT_DEVICE" ]         && _override_env REMOTEOCR_DEVICE         "$OPT_DEVICE"
[ -n "$OPT_ENGINE" ]         && _override_env REMOTEOCR_ENGINE         "$OPT_ENGINE"
[ -n "$OPT_STORAGE" ]        && _override_env REMOTEOCR_STORAGE        "$OPT_STORAGE"
[ -n "$OPT_SHUTDOWN_GRACE" ] && _override_env REMOTEOCR_SHUTDOWN_GRACE "$OPT_SHUTDOWN_GRACE"

# adiciona envs extras passados via --env
for e in "${EXTRA_ENVS[@]}"; do
  key="${e%%=*}"
  val="${e#*=}"
  _override_env "$key" "$val"
done

# ------------------------------------------------------------------ recria o container
echo "==> Parando e removendo container '$CONTAINER_NAME'..."
docker stop "$CONTAINER_NAME" 2>/dev/null || true
docker rm   "$CONTAINER_NAME" 2>/dev/null || true

echo "==> Criando novo container '$CONTAINER_NAME'..."

RUN_ARGS=(
  -d
  --name "$CONTAINER_NAME"
  --restart unless-stopped
  --gpus all
)

for vol in "${CONTAINER_VOLS[@]}" "${EXTRA_VOLS[@]}"; do
  RUN_ARGS+=(-v "$vol")
done

for env in "${CONTAINER_ENVS[@]}"; do
  RUN_ARGS+=(-e "$env")
done

RUN_ARGS+=("$IMAGE")

docker run "${RUN_ARGS[@]}"

echo ""
echo "Container '$CONTAINER_NAME' recriado com a imagem '$IMAGE'."
echo "Acompanhe os logs:"
echo "  docker logs -f $CONTAINER_NAME"
