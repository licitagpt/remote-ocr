using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using RemoteOcr.Client;
using RemoteOcr.Tools;

namespace RemoteOcr.Tests;

public sealed class ConvertRequestHandlerTests
{
    private static (string Directory, string PdfPath, JsonObject Header) Prepare(string id = "77-3", string md = "> __Página 1 de 1__\n\n[OCR:1]\n")
    {
        var root = TestOptions.TempDirectory();
        var directory = ConvertRequestHandler.RequestDirectory(Path.Combine(root, "tmp"), id);
        Directory.CreateDirectory(directory);
        var pdfPath = Path.Combine(directory, ConvertRequestHandler.SafePdfName("03-scan.pdf"));
        File.WriteAllBytes(pdfPath, "%PDF-1.7"u8.ToArray());
        var header = new JsonObject { ["type"] = "convert", ["id"] = id, ["pdf_name"] = "03-scan.pdf", ["md"] = md };
        return (directory, pdfPath, header);
    }

    private static OcrServerProcess Process(FakeChildProcess fake, int timeout = 5) =>
        new(() => fake, new OcrProcessOptions("python", 5, timeout), NullLogger.Instance, 1);

    [Theory]
    [InlineData("03-scan.pdf", "03-scan.pdf")]
    [InlineData("../../etc/passwd.pdf", "passwd.pdf")]
    [InlineData("C:\\pasta\\arquivo.PDF", "arquivo.PDF")]
    [InlineData("", "documento.pdf")]
    [InlineData("edital", "edital.pdf")]
    public void Nome_do_pdf_e_so_o_nome_do_arquivo(string input, string expected)
    {
        Assert.Equal(expected, ConvertRequestHandler.SafePdfName(input));
    }

    [Fact]
    public void Nome_com_caracteres_invalidos_vira_hifen()
    {
        var name = ConvertRequestHandler.SafePdfName("a\0b.pdf");
        Assert.DoesNotContain('\0', name);
        Assert.EndsWith(".pdf", name);
        Assert.Equal(Path.Combine("/t", "12-7"), ConvertRequestHandler.RequestDirectory("/t", "12-7"));
        Assert.Equal(Path.Combine("/t", "a_b"), ConvertRequestHandler.RequestDirectory("/t", "a/b"));
    }

    [Fact]
    public async Task Grava_o_md_manda_pdf_md_e_out_e_devolve_o_texto_do_out_sem_os_campos_locais()
    {
        var (directory, pdfPath, header) = Prepare();
        var fake = new FakeChildProcess(autoReady: true)
        {
            OnRequest = async request =>
            {
                Assert.Equal(pdfPath, (string?)request["pdf"]);
                Assert.Equal(Path.Combine(directory, "03-scan.md"), (string?)request["md"]);
                Assert.Equal(Path.Combine(directory, "03-scan.ocr.md"), (string?)request["out"]);
                Assert.Equal("> __Página 1 de 1__\n\n[OCR:1]\n", await File.ReadAllTextAsync((string)request["md"]!));
                await File.WriteAllTextAsync((string)request["out"]!, "> __Página 1 de 1__\n\nTexto reconhecido\n");
                return new JsonObject
                {
                    ["pdf"] = request["pdf"]?.DeepClone(), ["md"] = request["md"]?.DeepClone(), ["out"] = request["out"]?.DeepClone(),
                    ["ok"] = true, ["status"] = "ok", ["engine"] = "auto", ["backend"] = "rapidocr", ["pages"] = 1, ["ocr_pages"] = 1, ["ocr_done"] = 1,
                    ["empty_pages"] = 0, ["tables"] = 0, ["size_bytes"] = 8, ["md_size_bytes"] = 40, ["token_count"] = 9, ["tamanhoMd"] = 40,
                    ["warnings"] = new JsonArray("w1"), ["ms"] = new JsonObject { ["ocr"] = 700, ["total"] = 900 },
                };
            },
        };

        var (result, code) = await ConvertRequestHandler.RunAsync(header, pdfPath, Process(fake), CancellationToken.None);

        Assert.Null(code);
        Assert.Equal("result", (string?)result["type"]);
        Assert.Equal("77-3", (string?)result["id"]);
        Assert.Equal("convert_ocr#2", (string?)result["instance"]);
        Assert.True((bool?)result["ok"]);
        Assert.Equal("ok", (string?)result["status"]);
        Assert.Equal(9, (int?)result["token_count"]);
        Assert.Equal(900, (int?)result["ms"]!["total"]);
        Assert.Equal("w1", (string?)result["warnings"]![0]);
        Assert.Equal("> __Página 1 de 1__\n\nTexto reconhecido\n", (string?)result["md_text"]);
        Assert.Null(result["pdf"]);
        Assert.Null(result["md"]);
        Assert.Null(result["out"]);
        Assert.Null(result["tamanhoMd"]);

        Assert.True(ConvertRequestHandler.Cleanup(directory));
        Assert.False(Directory.Exists(directory));
    }

    [Fact]
    public async Task No_ocr_volta_sem_md_text()
    {
        var (_, pdfPath, header) = Prepare();
        var fake = new FakeChildProcess(autoReady: true) { OnRequest = _ => Task.FromResult<JsonObject?>(new JsonObject { ["ok"] = true, ["status"] = "no_ocr", ["pages"] = 5 }) };

        var (result, code) = await ConvertRequestHandler.RunAsync(header, pdfPath, Process(fake), CancellationToken.None);

        Assert.Null(code);
        Assert.Equal("no_ocr", (string?)result["status"]);
        Assert.Null(result["md_text"]);
    }

    [Fact]
    public async Task Erro_da_ferramenta_passa_com_codigo_e_mensagem()
    {
        var (_, pdfPath, header) = Prepare();
        var fake = new FakeChildProcess(autoReady: true)
        {
            OnRequest = _ => Task.FromResult<JsonObject?>(new JsonObject { ["ok"] = false, ["status"] = "error", ["error"] = new JsonObject { ["code"] = "MD_MISMATCH", ["message"] = "o .md tem 3 paginas e o PDF 4" } }),
        };

        var (result, code) = await ConvertRequestHandler.RunAsync(header, pdfPath, Process(fake), CancellationToken.None);

        Assert.Equal("MD_MISMATCH", code);
        Assert.False((bool?)result["ok"]);
        Assert.Equal("MD_MISMATCH", (string?)result["error"]!["code"]);
        Assert.Equal("o .md tem 3 paginas e o PDF 4", (string?)result["error"]!["message"]);
    }

    [Fact]
    public async Task Falha_do_processo_vira_result_com_o_codigo_da_ToolException()
    {
        var (_, pdfPath, header) = Prepare();
        var (crashResult, crashCode) = await ConvertRequestHandler.RunAsync(header, pdfPath, Process(new FakeChildProcess(autoReady: true) { OnRequest = _ => Task.FromResult<JsonObject?>(null) }, timeout: 1), CancellationToken.None);
        var (startResult, startCode) = await ConvertRequestHandler.RunAsync(header, pdfPath, Process(new FakeChildProcess(failToStart: true)), CancellationToken.None);

        Assert.Equal("TIMEOUT", crashCode);
        Assert.Equal("TIMEOUT", (string?)crashResult["error"]!["code"]);
        Assert.Equal("START", startCode);
        Assert.Equal("convert_ocr#2", (string?)startResult["instance"]);
        Assert.Equal("77-3", (string?)startResult["id"]);
    }

    [Fact]
    public async Task Ok_sem_o_arquivo_de_saida_e_erro_IO()
    {
        var (_, pdfPath, header) = Prepare();
        var fake = new FakeChildProcess(autoReady: true) { OnRequest = _ => Task.FromResult<JsonObject?>(new JsonObject { ["ok"] = true, ["status"] = "ok" }) };

        var (result, code) = await ConvertRequestHandler.RunAsync(header, pdfPath, Process(fake), CancellationToken.None);

        Assert.Equal("IO", code);
        Assert.Contains("não pôde ser lido", (string?)result["error"]!["message"]);
    }

    [Fact]
    public void Resumo_de_log()
    {
        var ok = new JsonObject { ["ok"] = true, ["status"] = "ok", ["pages"] = 12, ["ocr_done"] = 3, ["empty_pages"] = 1, ["token_count"] = 4567, ["engine"] = "auto" };
        var noOcr = new JsonObject { ["ok"] = true, ["status"] = "no_ocr", ["pages"] = 5 };
        var err = new JsonObject { ["ok"] = false, ["error"] = new JsonObject { ["code"] = "CRASH", ["message"] = "morreu" } };

        Assert.Equal($"OK     2.4s a.pdf 3/12 pág OCR (1 sem texto) · {4567:N0} tokens · auto", ConvertRequestHandler.Summary(ok, "a.pdf", TimeSpan.FromMilliseconds(2400)));
        Assert.Equal("OK    800ms b.pdf 5 pág · sem [OCR:n], nada a fazer", ConvertRequestHandler.Summary(noOcr, "b.pdf", TimeSpan.FromMilliseconds(800)));
        Assert.Equal("ERR    1.0s c.pdf CRASH: morreu", ConvertRequestHandler.Summary(err, "c.pdf", TimeSpan.FromSeconds(1)));
    }
}
