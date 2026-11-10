# convert_ocr

OCR das páginas que o [convert_pdf](../convert_pdf/README.md) marcou como `[OCR:n]`. Roda **uma vez por documento**: lê o
`.md` ao lado do PDF, faz o OCR só das páginas marcadas (as demais já estão em texto), remove cabeçalho/rodapé e
grava o resultado **de uma só vez, só depois que todas as páginas de OCR terminaram** (os marcadores `[OCR:n]`
desaparecem; o resto do documento sai byte a byte como estava). Qualquer erro deixa o original intacto.

Para onde vai o resultado é o parâmetro `OUTPUT` / `--output`:

| Valor | Efeito |
|---|---|
| `replace` | substitui o `.md` de entrada (escrita atômica: `.tmp` + rename) |
| `ocr-md` | grava `<nome>.ocr.md` ao lado e deixa o `.md` intacto |
| `auto` (default) | `ocr-md` no modo `local`, `replace` no `server` (o remote-ocr pede `out` explícito) |

`--out DIR` (local) e `"out"` (requisição do server) têm precedência e gravam onde mandarem; a validação `review`
sempre grava na pasta dela.

Três motores, escolhidos na linha de comando (`--engine`) ou por requisição no server-mode:

| Motor | O que faz | Custo por página (200 dpi, CPU → GPU RTX 3060) |
|---|---|---|
| `auto` (default) | O RapidOCR reconhece a página **uma vez**; o `pymupdf4llm` monta o markdown a partir dessas caixas e, **na página em que ele devolver tags HTML** (`<br>` em células de tabela, `<sup>`, `<span>`…), vale a montagem do `rapidocr`, feita das mesmas caixas, sem reconhecer de novo. Sempre com o reconhecedor RapidOCR (`--ocr-backend` é ignorado). As páginas trocadas contam em `html_pages` | ~ o do `pymupdf4llm` |
| `rapidocr` | PyMuPDF renderiza a página → [RapidOCR](https://github.com/RapidAI/RapidOCR) (PP-OCR, modelo `pt`) reconhece as linhas → montagem **nossa** do markdown (`core/boxes.py` + `convert_ocr.pdf.markdown`, copia do `convert_pdf.core.markdown`): parágrafos refluídos, listas, títulos (seções numeradas, caixa alta centrada, fonte maior) e tabelas simples (linhas com ≥ 2 células alinhadas) | ~2,2 s → ~0,8 s |
| `pymupdf4llm` | PyMuPDF renderiza → um reconhecedor gera a **camada de texto** (`--ocr-backend rapidocr`, default, ou `tesseract`) numa página limpa (imagem + texto invisível) → o [pymupdf4llm](https://github.com/pymupdf/RAG) com o modelo de layout `pymupdf.layout` monta o markdown (títulos, listas e tabelas vêm do modelo) | ~2,6 s → ~1,2 s |

Nos dois casos o cabeçalho/rodapé é o modelo do convert_pdf (`convert_ocr.pdf.hf`, cópia de `convert_pdf.core.hf`): repetição entre **todas** as páginas do
documento, texto nativo das páginas de texto + texto reconhecido das páginas de OCR, com similaridade difusa mais
tolerante (`--hf-sim 0.80`) porque o OCR erra letras. A primeira ocorrência de cada grupo é mantida, como no convert_pdf.

Página de OCR em que nada é reconhecido (foto, logotipo, assinatura) sai só com a paginação (`> __Página N de M__`) e
conta em `empty_pages`. Página escaneada deitada: se a maior parte do texto sai na vertical, a imagem é girada 90/270
graus e fica a melhor leitura (`rotated_pages`; `--no-auto-rotate` desliga; só com o reconhecedor RapidOCR).

## Uso

```bash
# instalar (venv do remote-ocr; o pacote e autocontido, nao depende do convert_pdf)
pip install -e "tools/convert_ocr[dev,layout]"        # [layout] = modelo de layout do motor pymupdf4llm

# linha de comando: um .pdf (com o .md ao lado) ou um diretorio inteiro; default grava <nome>.ocr.md ao lado;
# --output replace substitui o .md; --out DIR grava em outra arvore (testes)
python -m convert_ocr local <arquivo.pdf | diretorio> [--engine auto|rapidocr|pymupdf4llm] [--output replace|ocr-md] [--out DIR] [--md X.md] [--no-recursive] [--report PREFIXO] [--list LISTA.txt]

# server-mode (usado pelo remote-ocr): uma requisicao NDJSON por linha no stdin, uma resposta por linha no stdout
python -m convert_ocr server --engine auto --ocr-threads 2

# validacao (ver abaixo)
python -m convert_ocr review
```

Configuração: só por linha de comando, sem `.env` nem variáveis de ambiente. Todo parâmetro tem default (ver `--help`),
exceto `--storage`, obrigatório em todos os modos: a raiz de dados (`C:\app\licitagpt\storage` no desenvolvimento,
`/storage` em produção). Parâmetros principais: `--engine`, `--output` (`auto` | `replace` | `ocr-md`), `--ocr-backend`,
`--ocr-device` (`auto` | `cpu` | `cuda` | `dml`, ver [GPU](#gpu)), `--ocr-lang` (modelo de reconhecimento do RapidOCR, `pt`),
`--ocr-dpi` (200), `--ocr-threads` (threads do onnxruntime deste processo em CPU: com N processos use ~`cpu_count // N`),
`--timeout` (1800), `--max-ocr-pages` (1000), `--max-rss-mb` (4096: memória residente a partir da qual o server-mode encerra entre
documentos para ser reiniciado pelo remote-ocr; 0 desliga), `--tessdata`/`--tess-lang` (backend tesseract). O modelo de reconhecimento `pt`
do RapidOCR é baixado na primeira execução para `site-packages/rapidocr/models/`.

### GPU

Os dois motores inferem com o **onnxruntime** (o RapidOCR e o modelo de layout do pymupdf4llm) e usam a GPU
**por default quando ela está disponível** (`--ocr-device auto`). "Disponível" quer dizer que o pacote `onnxruntime`
instalado é a variante com GPU, porque a de CPU (a que vem com `pip install`) não tem o provider:

| Pacote | Provider | Onde | Quem usa |
|---|---|---|---|
| `onnxruntime` (default do pip) | CPU | todos | — |
| `onnxruntime-gpu` | CUDA (NVIDIA) | Windows e Linux | RapidOCR **e** modelo de layout |
| `onnxruntime-directml` | DirectML (qualquer GPU DirectX 12) | só Windows | só o RapidOCR (o `pymupdf.layout` só conhece CUDA) |

Com PyMuPDF 2 o `import pymupdf` ativaria o modelo de layout em CPU antes de o `convert_ocr` escolher o dispositivo;
o pacote bloqueia esse import no `__init__` (`convert_ocr.pdf.pymupdf_compat`) e `device.activate_layout` o libera no
dispositivo pedido (ver `../README.md`, "PyMuPDF 2"). O extrator de features desse modelo guarda em cache nativo,
sem limite, dados por tamanho de fonte: a camada de texto invisível usa tamanhos inteiros (`p4llm.font_size`), senão o
processo cresce ~4 MB por página de OCR; `--max-rss-mb` é a rede de segurança (ver "Protocolo do server-mode").

Os três instalam o mesmo módulo `onnxruntime`, por isso a troca é feita pelo script, não pelo `pip install` direto:

```bash
python scripts/setup_gpu.py --cuda     # desinstala o onnxruntime que houver, instala onnxruntime-gpu + CUDA/cuDNN via pip (nvidia-*)
python scripts/setup_gpu.py --dml      # onnxruntime-directml (Windows)
python scripts/setup_gpu.py --cpu      # volta para a CPU
python scripts/setup_gpu.py --check    # mostra o pacote, os providers e com qual deles o RapidOCR realmente carregou
```

Para o CUDA não é preciso instalar o CUDA Toolkit: o script lê a versão para a qual o `onnxruntime-gpu` foi compilado
(`onnxruntime.capi.build_and_package_info.cuda_version`, hoje CUDA 13) e instala os pacotes pip `nvidia-cuda-runtime`,
`nvidia-cublas`, `nvidia-cufft` e `nvidia-cudnn-cu13`; o `onnxruntime.preload_dlls()` carrega as DLLs de lá na subida.
Só o driver NVIDIA precisa estar instalado e suportar essa versão (`nvidia-smi` mostra a máxima). `pip check` passa a
reclamar que o `pymupdf-layout` exige `onnxruntime`: é cosmético, o módulo está lá.

No Linux o `preload_dlls()` não conhece o layout consolidado do CUDA 13 (`site-packages/nvidia/cu13/lib`), e sem essas
pastas no `LD_LIBRARY_PATH` a sessão CUDA falha e o processo segue em CPU (`note: sessoes do onnxruntime em
CPUExecutionProvider`). Na linha de comando exporte `LD_LIBRARY_PATH=<venv>/lib/python3.X/site-packages/nvidia/{cu13,cudnn,curand}/lib`;
sob o remote-ocr não é preciso: ele monta essa variável para o processo filho a partir do python do venv (`OcrProcessPool.BuildStartInfo`).

O que acontece na subida: o processo sonda os providers (`core/device.py`), carrega o RapidOCR já com `use_cuda`/`use_dml`,
faz uma inferência de aquecimento e lê das sessões o provider **efetivamente** em uso. Se a sessão CUDA falhar
(DLL ausente, driver antigo) ou cair para CPU, o processo avisa e segue em CPU; `--ocr-device cuda|dml` sem o provider
também vira aviso e CPU (nunca erro). O dispositivo aparece no log (`dispositivo: CUDAExecutionProvider | onnxruntime-gpu
1.31.0 | CUDA 13.0 | layout: cuda`), no evento `ready` do server-mode (`device`), no `.json` do `--report` e no rodapé
da tela de validação. `--ocr-gpu-id` escolhe a placa em máquinas com várias. O backend `tesseract` é sempre CPU
(Tesseract embutido no PyMuPDF).

Em GPU, `--ocr-threads` pesa pouco; com N processos do remote-ocr numa GPU só, cada processo tem seu contexto CUDA
(~300-500 MB de VRAM) e os modelos: uma RTX 3060 de 12 GB comporta folgadamente os 4-8 processos típicos.

### Backend `tesseract` (motor pymupdf4llm)

O PyMuPDF traz o Tesseract embutido, mas não os dados de idioma. `python scripts/get_tessdata.py --dir <pasta>` baixa
`por`, `eng` e `osd` (tessdata_fast); depois `--tessdata <pasta>` e `--ocr-backend tesseract`.

## Validação (`review`)

Aplicação web simples (biblioteca padrão do Python) para comparar, página a página, **como estava no PDF e como cada
motor tratou a página**:

```bash
python -m convert_ocr review [--port 8766] [--dir <pasta dos resultados>] [--no-open]
```

Os resultados ficam em `<storage>/review/ocr/<motor>/…` (`--dir` muda a raiz), um `.md` + `.json` por documento.
**O `.md` original nunca é alterado pela validação.** A varredura (na subida e em **Revarrer**) lê duas coisas:

- `<storage>/files`: documentos cujo `.md` ainda tem páginas `[OCR:n]` (candidatos a **Processar**);
- `<storage>/review/ocr/<motor>/`: resultados já gravados, de qualquer origem. As subpastas são os motores, inclusive
  os que esta ferramenta não roda (um `easyocr/` testado fora, por exemplo). Um resultado é `<nome>.md` ou
  `<nome>.ocr.md` (com preferência para o segundo quando há os dois) e, se houver, `<nome>.json` com as métricas; sem
  `.json` o resultado entra só com o markdown. O PDF de origem vem do campo `pdf` do `.json`, por isso tanto
  `<motor>/files/pncp/…` quanto `<motor>/pncp/…` servem;
- `<nome>.ocr.md` ao lado do PDF em `<storage>/files` (a saída default do `convert_ocr local`, sem `--out`): entra como
  o pseudo-motor **`.ocr.md`** (`ocr-md` na API), porque o arquivo não diz qual motor o gerou. Só cabe um por documento:
  para comparar dois motores use `--out`.

O fluxo normal é rodar os motores **antes**, em lote, sobre os casos de exemplo, e usar a tela só para comparar:

```bash
python -m convert_ocr local <storage>/files --list casos.txt --engine auto        --out <storage>/review/ocr/auto
python -m convert_ocr local <storage>/files --list casos.txt --engine rapidocr    --out <storage>/review/ocr/rapidocr
python -m convert_ocr local <storage>/files --list casos.txt --engine pymupdf4llm --out <storage>/review/ocr/pymupdf4llm
python -m convert_ocr review
```

(Sem `--out`, o `local` grava `<nome>.ocr.md` ao lado do PDF; a tela também o encontra, como `.ocr.md`.)

### Modo Documentos

Lista todos os documentos (filtro por PNCP/arquivo; "só com resultado" restringe aos já processados). Navegação pelas
páginas (◀ ▶, teclas ← →, "só páginas OCR"): imagem da página à esquerda; à direita as abas **.md atual** (o que o
convert_pdf deixou: `[OCR:n]` ou texto), uma por motor e **lado a lado**, com markdown renderizado ou bruto.
**Processar** roda o OCR do documento com o motor escolhido, em background, com progresso. Erros aparecem em modo
descritivo (código + mensagem do motor). Se o `.md` ao lado do PDF já foi substituído (sem `[OCR:n]`), as páginas de
OCR são deduzidas da camada de texto do PDF.

### Modo Lotes

Para não comparar tudo: **Novo lote** sorteia N páginas de OCR (seed opcional, reproduzível), uniformemente entre todas
as páginas de OCR dos documentos que têm resultado ok em **todos** os motores marcados, com no máximo 3 páginas por
documento enquanto houver alternativa. Cada item mostra a imagem da página e **uma coluna por motor** (layout ao lado
ou em cima, `⇅`); o veredito é opcional: **melhor motor** (teclas `1`..`n`), `0` empate, `x` nenhum, `c` limpa, `n`
observação; "avançar" pula para a próxima pendente, "só pendentes" faz ◀ ▶ pularem as revisadas. **Resumo** mostra a
contagem por veredito (e %) e as observações. Tudo fica em `<storage>/review/ocr/lotes.json`; **Excluir** apaga só o
lote, os resultados dos motores ficam. `#docs` / `#lotes` na URL abrem direto no modo.

## Protocolo do server-mode

Entrada (uma linha JSON): `{"id":"…","cmd":"convert","pdf":"files/pncp/…/x.pdf"}` e, opcionais, `"md"` (o `.md` do
convert_pdf; default ao lado do PDF), `"out"` (gravar o resultado em outro arquivo; sem ele vale `OUTPUT`, que no
server é `replace` por default) e `"engine"`.
`cmd` aceita `ocr` como sinônimo; também `ping`, `status`, `shutdown`. Caminhos relativos resolvem contra o storage.
Saída: `{"event":"ready",…}` na subida (com `output` = replace|ocr-md e `device`: `kind` cpu|cuda|dml, `provider`,
`active`, `layout`, `onnxruntime`, `cuda`, `note`) e, por documento, o resultado com `ok`, `status` (`ok` | `no_ocr` | `error`),
`engine`, `backend`, `pages`, `ocr_pages` (marcadores encontrados), `ocr_done`, `empty_pages`, `rotated_pages`,
`low_score_pages`, `html_pages` (motor `auto`: páginas em que o pymupdf4llm devolveu HTML e valeu a montagem do
rapidocr), `hf_removed`, `tables`, `headings`, `token_count`, `md_size_bytes`/`tamanhoMd`, `ms`.
Erros tipados: `NOT_FOUND`, `NO_MD`, `BAD_MD`, `MD_MISMATCH` (o `.md` não é deste PDF), `TOO_MANY_PAGES`, `NOT_PDF`,
`ENCRYPTED`, `OCR_FAILED`, `TIMEOUT`, `IO`, `INTERNAL`. `sys.stdout` é redirecionado para stderr na subida: as
bibliotecas de OCR usam `print()` e não podem corromper o protocolo.

Saídas do processo por conta própria: `{"event":"exit","reason":"watchdog"}` + código 3 quando uma chamada nativa
trava além de `--timeout` + 30 s (o documento em voo recebe `TIMEOUT`); `{"event":"exit","reason":"memory","retry":true,
"rss_mb":…,"limit_mb":…}` + código 4 quando, **depois** de responder um documento, a memória residente passou de
`--max-rss-mb` (`status` informa `rss_mb`). Nesse segundo caso um pedido que já tenha sido escrito fica sem resposta e
`retry` diz ao remote-ocr que pode reenviá-lo ao processo novo (nenhum documento começou a ser tratado).

Sinais: `SIGTERM`, `SIGINT` (o Ctrl+C no terminal do remote-ocr chega a todo o grupo de processos) e `SIGBREAK` (Windows) pedem o encerramento ordenado: o documento em voo termina e é respondido normalmente, a fila recebe `SHUTDOWN` e o processo sai com código 0. Nunca viram `KeyboardInterrupt` dentro de uma conversão.

Quem sobe este processo é o **remote-ocr** (.NET, `src/` deste repositório): ele mantém N processos `convert_ocr server`
(um por instância anunciada ao Workers), recebe do Workers por WebSocket o PDF e o `.md` do convert_pdf, grava os dois
numa pasta temporária (`<storage>/tmp/<id>/`), envia `{"cmd":"convert","pdf":…,"md":…,"out":"<nome>.ocr.md"}` a um
processo livre, lê o `out` e devolve o texto ao Workers (que grava o `.md` no lugar e atualiza `tokenCount`/`tamanhoMd`
do documento no `.json` do edital). `ping`/`status` alimentam a mensagem `health` do protocolo. Os parâmetros da
ferramenta vêm das opções do remote-ocr (`--device` → `--ocr-device`, `--engine`, `--args` para o resto); ver o README
na raiz do repositório.

## Pastas

| Pasta | Conteúdo |
|---|---|
| `src/convert_ocr/core/` | `mdfile.py` (ler/remontar o `.md`), `boxes.py` (caixas → linhas → markdown), `device.py` (CPU/CUDA/DirectML: escolha, DLLs do CUDA, provider efetivo, `pymupdf.layout` em GPU), `engines/` (`auto.py`, `rapid.py`, `p4llm.py`, base com o reconhecedor, o aquecimento, o auto-rotate e o detector de HTML), `document.py` (pipeline), `metrics.py` |
| `src/convert_ocr/runtime/` | execução: `local` (CLI), `server` (NDJSON), engine |
| `src/convert_ocr/review/` | validação: `corpus.py` (varredura do storage e dos resultados, páginas de OCR, sorteio), `store.py` (lotes e vereditos em `lotes.json`), `server.py` (HTTP) e `static/index.html` |
| `tests/` | pytest; `-m "not ocr"` roda só o que não precisa do OCR real; `layout` e `tesseract` marcam os motores opcionais |
| `scripts/` | `get_tessdata.py`, `setup_gpu.py` (troca o onnxruntime: `--cuda` / `--dml` / `--cpu` / `--check`) |
