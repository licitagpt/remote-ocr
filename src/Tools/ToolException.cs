namespace RemoteOcr.Tools;

/// <summary>
/// Erro definitivo num pedido ao convert_ocr: ele respondeu "ok": false (código NOT_FOUND, NO_MD, MD_MISMATCH,
/// TOO_MANY_PAGES, NOT_PDF, ENCRYPTED, OCR_FAILED, TIMEOUT, IO, INTERNAL...), o processo morreu durante o pedido
/// (CRASH), não respondeu no prazo e foi morto (TIMEOUT) ou nem chegou a iniciar (START — Python ou pacote mal
/// configurados). O código vai para o Workers em `result.error.code`.
/// </summary>
public sealed class ToolException(string code, string message) : Exception($"{code}: {message}")
{
    public string Code { get; } = code;

    public string Detail { get; } = message;
}
