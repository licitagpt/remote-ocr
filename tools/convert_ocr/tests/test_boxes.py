"""Caixas do OCR -> linhas visuais, celulas, blocos, titulos e tabelas (sem rodar o OCR)."""

from __future__ import annotations

from convert_ocr.core import boxes as bx


def box(x0, y0, x1, y1, text, score=0.99):
    return bx.OcrBox(x0, y0, x1, y1, text, score)


def quad(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def test_to_boxes_scales_and_filters():
    quads = [quad(100, 100, 400, 130), quad(100, 200, 400, 230), quad(50, 300, 60, 500)]
    out = bx.to_boxes(quads, ["bom", "ruim", "ab"], [0.9, 0.3, 0.9], scale=2.0, min_score=0.5)
    assert [b.text for b in out] == ["bom", "ab"]
    assert (out[0].x0, out[0].y0, out[0].x1, out[0].y1) == (50, 50, 200, 65)
    assert not out[0].vertical
    # caixa cuja 1a aresta e vertical (texto deitado)
    v = bx.to_boxes([[[50, 300], [50, 500], [60, 500], [60, 300]]], ["lateral"], [0.9], 1.0, 0.5)[0]
    assert v.vertical


def test_rows_cells_and_paragraph_blocks():
    bs = [
        box(200, 50, 400, 70, "TERMO DE DISPENSA"),                       # titulo centrado em caixa alta
        box(60, 100, 540, 118, "Fica dispensada a realizacao do certame licitatorio para contratacao"),
        box(60, 120, 540, 138, "de empresa especializada no fornecimento de refeicoes e lanches para"),
        box(60, 140, 300, 158, "atender as necessidades da Camara."),
        box(60, 200, 540, 218, "Outro paragrafo depois de um vao maior, com texto suficiente para linha."),
        box(60, 260, 100, 278, "Item"), box(300, 260, 400, 278, "Valor"),  # 2 celulas na mesma linha visual
    ]
    rows = bx.to_rows(bs)
    assert [len(r) for r in rows] == [1, 1, 1, 1, 1, 2]
    lines, line_rows, body = bx.rows_to_lines(rows, [])
    assert len(lines) == 7 and body >= 9.0
    blocks = [l.block for l in lines]
    assert blocks[0] != blocks[1]                      # titulo em bloco proprio
    assert blocks[1] == blocks[2] == blocks[3]         # paragrafo
    assert blocks[4] != blocks[3]                      # vao -> bloco novo
    assert blocks[5] == blocks[6] != blocks[4]         # linha de celulas em bloco proprio
    assert lines[0].all_bold and not lines[1].all_bold
    md, nh, nt = bx.page_markdown(lines, line_rows, body, 150)
    assert nt == 0 and md.startswith("### TERMO DE DISPENSA")
    assert "certame licitatorio para contratacao de empresa especializada" in md   # linhas refluidas no paragrafo


def test_merge_fragments_of_same_line():
    bs = [box(60, 100, 200, 118, "do servico"), box(206, 100, 400, 118, "publico municipal"),
          box(410, 100, 540, 118, "de Itaja")]
    rows = bx.to_rows(bs)
    assert len(rows) == 1 and len(rows[0]) == 1 and rows[0][0].text == "do servico publico municipal de Itaja"


def test_numbered_section_becomes_heading():
    bs = [box(60, 50, 260, 68, "5. Levantamento de Mercado"),
          box(60, 75, 540, 93, "A Instrucao Normativa preve variadas possibilidades de levantamento de precos."),
          box(60, 95, 540, 113, "Realizou-se uma vasta pesquisa de mercado com o intuito de encontrar empresas.")]
    lines, line_rows, body = bx.rows_to_lines(bx.to_rows(bs), [])
    md, nh, _ = bx.page_markdown(lines, line_rows, body, 150)
    assert nh == 1 and md.startswith("## 5. Levantamento de Mercado")


def test_detect_table_with_continuation_rows():
    y = 100
    bs = []
    header = ["Item", "Descricao", "Und", "Valor"]
    cols = [60, 110, 330, 420]
    cw = 5.5                                                   # largura de um caractere a 10pt
    for c, t in enumerate(header):
        bs.append(box(cols[c], y, cols[c] + cw * len(t), y + 16, t))
    for i in range(1, 5):
        y += 20
        vals = [f"{i}", f"Servico numero {i} de pavimentacao", "m2", f"{i * 125},50"]
        for c, t in enumerate(vals):
            bs.append(box(cols[c], y, cols[c] + cw * len(t), y + 16, t))
        if i == 2:                                              # texto da celula que quebrou na linha de baixo
            y += 18
            bs.append(box(cols[1], y, cols[1] + cw * 13, y + 14, "em via urbana"))
    bs.append(box(60, y + 60, 540, y + 78, "Observacoes gerais sobre a planilha acima, em paragrafo normal."))
    rows = bx.to_rows(bs)
    lines, line_rows, body = bx.rows_to_lines(rows, [])
    tables = bx.detect_tables(line_rows)
    assert len(tables) == 1
    md = tables[0][1]
    assert md.splitlines()[0] == "| Item | Descricao | Und | Valor |"
    assert "| 2 | Servico numero 2 de pavimentacao em via urbana | m2 | 250,50 |" in md
    page_md, _, nt = bx.page_markdown(lines, line_rows, body, 150)
    assert nt == 1 and "Observacoes gerais" in page_md and page_md.index("| Item") < page_md.index("Observacoes")


def test_two_cell_rows_alone_are_not_a_table():
    bs = [box(60, 50, 200, 66, "ESTADO DO RN"), box(500, 50, 540, 66, "Fl.:"),
          box(60, 100, 540, 118, "Paragrafo normal de texto com tamanho suficiente para ser linha cheia aqui.")]
    lines, line_rows, body = bx.rows_to_lines(bx.to_rows(bs), [])
    assert bx.detect_tables(line_rows) == []


def test_vertical_share_and_quality():
    bs = [bx.OcrBox(0, 0, 10, 100, "lateral", 0.9, True), box(0, 0, 100, 10, "abc", 0.5)]
    assert abs(bx.vertical_share(bs) - 7 / 10) < 1e-9
    assert abs(bx.quality(bs) - (0.9 * 7 + 0.5 * 3)) < 1e-9
