# remote-ocr

Cliente de OCR do licitagpt. Roda em qualquer máquina (de preferência com GPU NVIDIA), em docker, e se conecta ao
serviço **Workers** por WebSocket para fazer o OCR das páginas que o `convert_pdf` marcou como `[OCR:n]` nos `.md`
dos editais. Cada cliente sobe N processos Python (`convert_ocr server`) e anuncia N instâncias ao Workers; a
capacidade do worker `convert_ocr` do Workers é a soma das instâncias de todos os clientes conectados — quantos
quiser, entrando e saindo a qualquer momento.

| Pasta | Conteúdo |
|---|---|
| `src/` | O cliente .NET 10 (`RemoteOcr.csproj`): conexão WebSocket, pool de processos Python, protocolo. |
| `tests/` | xUnit v3. |
| `tools/convert_ocr/` | A ferramenta Python de OCR (RapidOCR \| pymupdf4llm), server-mode NDJSON por stdin/stdout. Autocontida: os módulos que ela compartilhava com o `convert_pdf` do repositório workers estão copiados em `src/convert_ocr/pdf/` (cópia manual; ver o `__init__` de lá). Ver `tools/convert_ocr/README.md`. |

## Como funciona

1. Na subida, o cliente inicia os N processos `python -m convert_ocr server` em paralelo (cada um carrega os modelos e
   aquece a GPU: até `--startup-timeout`, 180 s) e, ao mesmo tempo, conecta ao Workers em
   `ws://HOST:PORTA/ws/remote-ocr` com o header `X-Api-Key` (a mesma chave em todos os clientes:
   `Security:ApiKey` no Workers). Se a conexão falhar, tenta de novo com backoff (1 s → 30 s; chave
   recusada: 60 s), para sempre.
2. Manda `hello` com o nome, a versão, as instâncias prontas e o dispositivo (`device.active=CUDAExecutionProvider`);
   o Workers responde `hello_ok` e passa a mandar documentos, no máximo um por instância livre. Processos que sobem
   ou caem depois geram `instances` (a capacidade no Workers acompanha).
3. Cada documento chega como uma mensagem binária: cabeçalho JSON (`id`, `pdf_name`, `md` = o `.md` do convert_pdf)
   + os bytes do PDF, gravados direto em `<storage>/tmp/<id>/`. Um processo livre recebe
   `{"cmd":"convert","pdf","md","out"}`, o `.md` final (`out`) é lido e volta ao Workers num `result` com todos os campos
   do convert_ocr (`status`, `pages`, `ocr_done`, `token_count`, `ms`...) e o texto em `md_text`. A pasta temporária é
   apagada. Quem grava o `.md` no storage e atualiza o `.json` do edital é o Workers.
4. `health` do Workers → `health_result` com o `status` de cada processo (rss, documentos, erros, em voo); `cancel`
   com `kill` mata o processo travado (reinicia no próximo documento); `bye` do Workers = desligamento, reconecta
   quando ele voltar.
5. Se a conexão cai no meio de um documento, o Workers devolve o item à fila e outro cliente o pega; aqui o processo
   termina o documento e o resultado é descartado. No desligamento (SIGTERM/Ctrl+C) o cliente manda `bye`, espera
   os documentos em voo por até `--shutdown-grace` (90 s), fecha e encerra os processos Python.

Erros de um documento vão ao Workers com código: os da ferramenta (`NOT_FOUND`, `NO_MD`, `BAD_MD`, `MD_MISMATCH`,
`TOO_MANY_PAGES`, `NOT_PDF`, `ENCRYPTED`, `OCR_FAILED`, `TIMEOUT`, `IO`, `INTERNAL`) e os do cliente (`CRASH` processo
morreu, `TIMEOUT` sem resposta em `--request-timeout`, `START` processo não reinicia, `BUSY` sem processo livre). A
reciclagem por memória da ferramenta (`--max-rss-mb`) é transparente: o pedido é reenviado ao processo novo.

## Configuração

Linha de comando com fallback em variáveis de ambiente (`REMOTEOCR_*`); no docker use só as variáveis.

| Opção | Variável | Padrão | O que é |
|---|---|---|---|
| `--host` | `REMOTEOCR_HOST` | obrigatório | `host:porta`, `ws://host:porta` ou `wss://host` (path padrão `/ws/remote-ocr`; `http(s)://` também vale). A porta é a da API do Workers. |
| `--api_key` | `REMOTEOCR_API_KEY` | obrigatório | `Security:ApiKey` do Workers (a mesma chave `X-Api-Key` da API) |
| `--instances` | `REMOTEOCR_INSTANCES` | 1 | processos Python = instâncias anunciadas (~300–500 MB de VRAM cada; uma RTX 3060 12 GB aguenta 4–8) |
| `--device` | `REMOTEOCR_DEVICE` | `auto` | `auto` \| `cpu` \| `cuda` \| `dml` → `--ocr-device` da ferramenta |
| `--engine` | `REMOTEOCR_ENGINE` | `auto` | `auto` \| `rapidocr` \| `pymupdf4llm` |
| `--python` | `REMOTEOCR_PYTHON` | `../tools/.venv/bin/python` se existir, senão `python` | interpretador com o convert_ocr instalado |
| `--tool-dir` | `REMOTEOCR_TOOL_DIR` | `../tools/convert_ocr` | pasta de trabalho dos processos |
| `--storage` | `REMOTEOCR_STORAGE` | `../storage` | logs da ferramenta (`logs/`) e temporários (`tmp/<id>/`) |
| `--args` | `REMOTEOCR_ARGS` | | extras para a ferramenta, ex.: `--ocr-threads 2 --max-rss-mb 4096` |
| `--name` | `REMOTEOCR_NAME` | nome da máquina | nome do cliente nos logs do Workers |
| `--startup-timeout` / `--request-timeout` / `--shutdown-grace` | `REMOTEOCR_STARTUP_TIMEOUT` / `_REQUEST_TIMEOUT` / `_SHUTDOWN_GRACE` | 180 / 1830 / 90 s | |
| | `REMOTEOCR_LOG_LEVEL` | `Information` | `Debug` mostra o stderr dos processos Python |

Códigos de saída: 2 configuração inválida (a mensagem de uso vai para o stderr), 3 nenhum processo Python subiu
(o docker reinicia o container com `--restart unless-stopped`).

## Docker

```bash
docker build -t licitagpt/remote-ocr .                        # CUDA 13 via pip (onnxruntime-gpu + nvidia-*)
docker build -t licitagpt/remote-ocr --build-arg DEVICE=cpu .  # só CPU

docker run -d --name remote-ocr --gpus all --restart unless-stopped --stop-timeout 120 \
  -e REMOTEOCR_HOST=186.216.247.55:28747 \
  -e REMOTEOCR_API_KEY=MINHA_SENHA_SECRETA \
  -e REMOTEOCR_INSTANCES=4 \
  -e REMOTEOCR_DEVICE=cuda \
  -v remote-ocr-data:/data \
  licitagpt/remote-ocr

docker logs -f remote-ocr      # "convert_ocr#1: convert_ocr 0.3.0 pronto (pid 17): ... device.active=CUDAExecutionProvider"
                               # "conectado a ws://186.216.247.55:28747/ws/remote-ocr como 'gpu-01': 4 instância(s) anunciada(s)"
```

GPU: só o driver NVIDIA e o [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/)
precisam estar na máquina (`nvidia-smi` dentro do container deve funcionar); CUDA/cuDNN vêm nos pacotes pip da
imagem. Sem GPU, `REMOTEOCR_DEVICE=cpu` (e `REMOTEOCR_ARGS="--ocr-threads N"`, com N ≈ núcleos / instâncias).
A imagem já traz os modelos do RapidOCR baixados (`scripts/prefetch_models.py`). `--stop-timeout 120` dá tempo ao
`REMOTEOCR_SHUTDOWN_GRACE` (90 s) de terminar os documentos em voo antes do SIGKILL.

Em produção prefira `wss://` (TLS num proxy reverso na frente do Workers): com `ws://` a chave e os PDFs trafegam em claro.

## Rodando sem docker (desenvolvimento)

```bash
python3 -m venv tools/.venv
tools/.venv/bin/pip install -e "tools/convert_ocr[dev,layout]"          # Windows: tools/.venv/Scripts/pip
tools/.venv/bin/python tools/convert_ocr/scripts/setup_gpu.py --cuda    # opcional: GPU NVIDIA (--check para conferir)
cd src && dotnet run -- --host localhost:43210 --api_key dev-secret --instances 1 --device cuda
```

No Workers (repositório `workers`), `Security__ApiKey=dev-secret` no `.env`; o `dotnet run` de lá escuta em
`http://localhost:43210` (launchSettings) e o log mostra `cliente <nome> conectado: 1 instância(s) ... total 1 instância(s) em 1 cliente(s)`.

## Testes

```bash
dotnet test tests/RemoteOcr.Tests.csproj
tools/.venv/bin/python -m pytest tools/convert_ocr -m "not ocr"   # rápido; sem o marcador roda o OCR de verdade
```

## Protocolo (v1)

Text frames JSON com `type`; a única mensagem binária é o `convert`. Autenticação no upgrade HTTP (`X-Api-Key` ou
`?api_key=`); chave errada = HTTP 401, chave não configurada no servidor = 503.

| Direção | type | Campos |
|---|---|---|
| cliente → servidor | `hello` | `protocol: 1, name, version, instances, device{kind, active, provider...}, tool_version, engine, hostname, pid` |
| servidor → cliente | `hello_ok` | `protocol, server, total_instances, request_timeout_s, max_pdf_mb, max_result_mb` |
| servidor → cliente | `reject` | `code (PROTOCOL \| BAD_HELLO), message` (e fecha 1008) |
| servidor → cliente | `convert` (binário) | `u32 BE tamanho do cabeçalho` \| cabeçalho JSON `{type, id, pdf_name, md, timeout_s}` \| bytes do PDF |
| cliente → servidor | `result` | `id, instance, ok, status, engine, backend, pages, ocr_pages, ocr_done, empty_pages, rotated_pages, low_score_pages, html_pages, lines, hf_groups, hf_removed, tables, headings, chars, size_bytes, md_size_bytes, token_count, warnings, ms{...}, md_text?, error?{code, message}` |
| cliente → servidor | `instances` | `instances, reason` |
| servidor → cliente | `cancel` | `id, kill` |
| servidor → cliente | `health` | `id` |
| cliente → servidor | `health_result` | `id, ok, free, in_flight, instances_total, uptime_s, version, instances: [{index, label, pid, ready, busy, starts, rss_mb, max_rss_mb, done, errors, timeouts, ocr_pages, uptime_s, inflight_s}]` |
| ambos | `ping` / `pong` | `ts` |
| ambos | `bye` | `reason` |

Keepalive é o ping/pong nativo do WebSocket (20 s / 60 s dos dois lados): um peer morto é detectado em ~1 min.
A implementação do lado do Workers está em `src/Workers/ConvertOcr/` do repositório workers (`RemoteOcrHub`,
`RemoteOcrConnection`, `RemoteOcrEndpoint`, `RemoteOcrProtocol`); aqui em `src/Protocol/` e `src/Client/`.
