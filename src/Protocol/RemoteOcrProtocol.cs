using System.Buffers.Binary;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace RemoteOcr.Protocol;

/// <summary>
/// Protocolo entre o Workers (servidor) e este cliente, versão 1 — espelho de Workers/ConvertOcr/RemoteOcrProtocol.cs
/// no repositório workers (cópia deliberada: repositórios e deploys separados). Mensagens de controle são text frames
/// JSON com "type"; a única binária é o `convert` do servidor: u32 big-endian com o tamanho do cabeçalho | cabeçalho
/// JSON UTF-8 {type:"convert", id, pdf_name, md, timeout_s} | bytes do PDF.
///
/// cliente → servidor: hello, instances, result, health_result, ping/pong, bye.
/// servidor → cliente: hello_ok, reject, convert (binário), cancel {id, kill}, health {id}, ping/pong, bye.
/// </summary>
public static class RemoteOcrProtocol
{
    public const int Version = 1;

    public const string Path = "/ws/remote-ocr";

    public const string ApiKeyHeader = "X-Api-Key";

    public static readonly JsonSerializerOptions JsonOptions = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    public static class Types
    {
        public const string Hello = "hello";
        public const string HelloOk = "hello_ok";
        public const string Reject = "reject";
        public const string Convert = "convert";
        public const string Result = "result";
        public const string Cancel = "cancel";
        public const string Health = "health";
        public const string HealthResult = "health_result";
        public const string Instances = "instances";
        public const string Ping = "ping";
        public const string Pong = "pong";
        public const string Bye = "bye";
    }

    public static byte[] EncodeText(JsonObject message) => Encoding.UTF8.GetBytes(message.ToJsonString(JsonOptions));

    public static byte[] EncodeBinaryHeader(JsonObject header)
    {
        var json = Encoding.UTF8.GetBytes(header.ToJsonString(JsonOptions));
        var result = new byte[4 + json.Length];
        BinaryPrimitives.WriteUInt32BigEndian(result, (uint)json.Length);
        json.CopyTo(result, 4);
        return result;
    }

    /// <summary>
    /// Tenta ler o cabeçalho de uma mensagem binária a partir dos bytes já recebidos. Devolve false enquanto faltarem
    /// bytes; <paramref name="consumed"/> é quantos bytes do início pertencem ao cabeçalho. Lança FormatException se o
    /// cabeçalho não for um objeto JSON ou for maior que <paramref name="maxHeaderBytes"/>.
    /// </summary>
    public static bool TryDecodeBinaryHeader(ReadOnlySpan<byte> buffer, out JsonObject? header, out int consumed, int maxHeaderBytes = 64 * 1024 * 1024)
    {
        header = null;
        consumed = 0;
        if (buffer.Length < 4)
        {
            return false;
        }

        var length = BinaryPrimitives.ReadUInt32BigEndian(buffer);
        if (length > (uint)maxHeaderBytes)
        {
            throw new FormatException($"cabeçalho binário de {length} bytes excede o limite de {maxHeaderBytes}");
        }

        if (buffer.Length < 4 + (int)length)
        {
            return false;
        }

        var json = Encoding.UTF8.GetString(buffer.Slice(4, (int)length));
        try
        {
            header = JsonNode.Parse(json) as JsonObject ?? throw new FormatException("cabeçalho binário não é um objeto JSON");
        }
        catch (JsonException ex)
        {
            throw new FormatException($"cabeçalho binário não é JSON válido: {ex.Message}", ex);
        }

        consumed = 4 + (int)length;
        return true;
    }

    public static JsonObject? ParseText(ReadOnlySpan<byte> payload)
    {
        try
        {
            return JsonNode.Parse(Encoding.UTF8.GetString(payload)) as JsonObject;
        }
        catch (JsonException)
        {
            return null;
        }
    }
}
