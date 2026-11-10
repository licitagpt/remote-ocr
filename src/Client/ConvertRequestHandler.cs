using System.Text;
using System.Text.Json.Nodes;
using RemoteOcr.Protocol;
using RemoteOcr.Tools;

namespace RemoteOcr.Client;

/// <summary>
/// Um `convert` do Workers de ponta a ponta no disco local: o PDF já foi gravado em &lt;storage&gt;/tmp/&lt;id&gt;/&lt;pdf_name&gt; pelo
/// laço de recepção; aqui o `.md` do convert_pdf (campo `md` do cabeçalho) vai para &lt;stem&gt;.md ao lado, o processo
/// recebe {cmd:"convert", pdf, md, out:&lt;stem&gt;.ocr.md}, o `out` é lido de volta e vira `md_text` no `result`. Erros da
/// ferramenta ou do processo (ToolException) viram `result {ok:false, error{code,message}}`. A pasta temporária é
/// apagada por <see cref="Cleanup"/> no fim, com ou sem erro.
/// </summary>
public static class ConvertRequestHandler
{
    private static readonly UTF8Encoding Utf8NoBom = new(encoderShouldEmitUTF8Identifier: false);

    /// <summary>Campos da resposta do convert_ocr que não fazem sentido para o Workers (caminhos locais, aliases).</summary>
    private static readonly HashSet<string> DroppedFields = ["pdf", "md", "out", "id", "tamanhoMd"];

    /// <summary>Nome de arquivo seguro para o PDF recebido (só o nome, sem pastas; caracteres inválidos viram "-").</summary>
    public static string SafePdfName(string? pdfName)
    {
        // Só o nome: o Workers manda o nome do arquivo, mas um caminho (Windows ou Linux) nunca pode sair da pasta do pedido.
        var raw = pdfName ?? "";
        var name = raw[(Math.Max(raw.LastIndexOf('/'), raw.LastIndexOf('\\')) + 1)..];
        if (string.IsNullOrWhiteSpace(name) || name is "." or "..")
        {
            name = "documento.pdf";
        }

        var invalid = Path.GetInvalidFileNameChars();
        var builder = new StringBuilder(name.Length);
        foreach (var c in name)
        {
            builder.Append(invalid.Contains(c) ? '-' : c);
        }

        name = builder.ToString();
        return name.EndsWith(".pdf", StringComparison.OrdinalIgnoreCase) ? name : name + ".pdf";
    }

    /// <summary>Pasta temporária de um pedido.</summary>
    public static string RequestDirectory(string tempRoot, string id)
    {
        var safeId = string.Concat(id.Select(c => char.IsLetterOrDigit(c) || c is '-' or '_' ? c : '_'));
        return Path.Combine(tempRoot, safeId);
    }

    /// <summary>
    /// Roda o documento num processo já reservado. Devolve a mensagem `result` pronta para enviar e o código de erro
    /// (null em sucesso) — START avisa o pool que o processo não reiniciou.
    /// </summary>
    public static async Task<(JsonObject Result, string? ErrorCode)> RunAsync(JsonObject header, string pdfPath, OcrServerProcess process, CancellationToken cancellationToken)
    {
        var id = (string?)header["id"] ?? "";
        var directory = Path.GetDirectoryName(pdfPath)!;
        var stem = Path.GetFileNameWithoutExtension(pdfPath);
        var mdPath = Path.Combine(directory, stem + ".md");
        var outPath = Path.Combine(directory, stem + ".ocr.md");

        try
        {
            await File.WriteAllTextAsync(mdPath, (string?)header["md"] ?? "", Utf8NoBom, cancellationToken);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            return (ErrorResult(id, process, "IO", $"não foi possível gravar o .md temporário: {ex.Message}"), "IO");
        }

        var request = new JsonObject
        {
            ["cmd"] = "convert",
            ["pdf"] = pdfPath,
            ["md"] = mdPath,
            ["out"] = outPath,
        };
        if ((string?)header["engine"] is { Length: > 0 } engine)
        {
            request["engine"] = engine;
        }

        JsonObject response;
        try
        {
            response = await process.SendAsync(request, cancellationToken);
        }
        catch (ToolException ex)
        {
            return (ErrorResult(id, process, ex.Code, ex.Detail), ex.Code);
        }

        var result = BuildResult(id, process, response);
        var ok = (bool?)response["ok"] ?? false;
        if (!ok)
        {
            return (result, (string?)(response["error"] as JsonObject)?["code"] ?? "ERROR");
        }

        if ((string?)response["status"] == "ok")
        {
            try
            {
                result["md_text"] = await File.ReadAllTextAsync(outPath, Encoding.UTF8, cancellationToken);
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
            {
                return (ErrorResult(id, process, "IO", $"convert_ocr respondeu ok mas o .md de saída não pôde ser lido: {ex.Message}"), "IO");
            }
        }

        return (result, null);
    }

    /// <summary>Copia os campos do OcrResult da ferramenta para o `result` do protocolo.</summary>
    public static JsonObject BuildResult(string id, OcrServerProcess process, JsonObject response)
    {
        var result = new JsonObject
        {
            ["type"] = RemoteOcrProtocol.Types.Result,
            ["id"] = id,
            ["instance"] = process.Label,
        };
        foreach (var (key, value) in response)
        {
            if (!DroppedFields.Contains(key))
            {
                result[key] = value?.DeepClone();
            }
        }

        result["ok"] = (bool?)response["ok"] ?? false;
        return result;
    }

    public static JsonObject ErrorResult(string id, OcrServerProcess? process, string code, string message) => new()
    {
        ["type"] = RemoteOcrProtocol.Types.Result,
        ["id"] = id,
        ["instance"] = process?.Label,
        ["ok"] = false,
        ["status"] = "error",
        ["error"] = new JsonObject { ["code"] = code, ["message"] = message },
    };

    /// <summary>Apaga a pasta temporária do pedido; falhas só em Debug (um arquivo preso não pode derrubar o cliente).</summary>
    public static bool Cleanup(string directory)
    {
        try
        {
            if (Directory.Exists(directory))
            {
                Directory.Delete(directory, recursive: true);
            }

            return true;
        }
        catch (Exception)
        {
            return false;
        }
    }

    /// <summary>Linha de log de um documento: "OK 12.3s 03-scan.pdf 3/12 pág · 4.567 tokens · auto" ou "ERR 0.4s x.pdf CODE: msg".</summary>
    public static string Summary(JsonObject result, string pdfName, TimeSpan elapsed)
    {
        var time = elapsed.TotalSeconds >= 1 ? $"{elapsed.TotalSeconds:0.0}s" : $"{elapsed.TotalMilliseconds:0}ms";
        if ((bool?)result["ok"] != true)
        {
            var error = result["error"] as JsonObject;
            return $"ERR {time,7} {pdfName} {(string?)error?["code"]}: {(string?)error?["message"]}";
        }

        var status = (string?)result["status"];
        if (status == "no_ocr")
        {
            return $"OK  {time,7} {pdfName} {(int?)result["pages"] ?? 0} pág · sem [OCR:n], nada a fazer";
        }

        var empty = ((int?)result["empty_pages"] ?? 0) is var e and > 0 ? $" ({e} sem texto)" : "";
        return $"OK  {time,7} {pdfName} {(int?)result["ocr_done"] ?? 0}/{(int?)result["pages"] ?? 0} pág OCR{empty} · {(int?)result["token_count"] ?? 0:N0} tokens · {(string?)result["engine"]}";
    }
}
