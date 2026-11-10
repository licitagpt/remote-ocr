"""Modulos compartilhados copiados do convert_pdf (tools/convert_pdf do repositorio workers), para o convert_ocr nao depender
daquele pacote no remote-ocr: `pymupdf_compat` (bloqueio do pymupdf.layout/pymupdf4llm no import), `textpage` (linhas e
spans), `hf` (modelo de cabecalho/rodape), `markdown` (linhas -> markdown, tabelas), `tokens` (tiktoken), `procmem` (RSS)
e `stdin_reader` (leitura do stdin no server-mode).

Copia MANUAL, feita em 2026-11-09 a partir do convert_pdf 1.x: correcoes futuras nesses modulos no convert_pdf precisam
ser portadas aqui (e vice-versa). A unica alteracao nas copias e o import interno `convert_pdf.core.textpage` ->
`convert_ocr.pdf.textpage` (hf, markdown) e o nome do logger em tokens.
"""
