namespace RemoteOcr.Tests;

internal static class TestOptions
{
    /// <summary>Opções mínimas para testes que não sobem o Python de verdade.</summary>
    public static RemoteOcrOptions Create(string storage, int instances = 1, string host = "ws://127.0.0.1:1/ws/remote-ocr", int requestTimeoutSeconds = 5, int startupTimeoutSeconds = 5) => new()
    {
        Host = new Uri(host),
        ApiKey = "chave",
        Instances = instances,
        Python = "python-falso",
        ToolDir = storage,
        Storage = storage,
        Name = "teste-01",
        Device = "cpu",
        RequestTimeoutSeconds = requestTimeoutSeconds,
        StartupTimeoutSeconds = startupTimeoutSeconds,
        ShutdownGraceSeconds = 5,
    };

    public static string TempDirectory()
    {
        var path = Path.Combine(Path.GetTempPath(), "remote-ocr-tests", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(path);
        return path;
    }
}
