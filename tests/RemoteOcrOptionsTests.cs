namespace RemoteOcr.Tests;

public sealed class RemoteOcrOptionsTests
{
    private static string? NoEnv(string _) => null;

    private static Func<string, string?> Env(params (string Key, string Value)[] pairs) =>
        key => pairs.FirstOrDefault(p => p.Key == key).Value;

    [Fact]
    public void Linha_de_comando_tem_precedencia_sobre_o_ambiente_e_os_defaults()
    {
        var env = Env(("REMOTEOCR_HOST", "10.0.0.1:1000"), ("REMOTEOCR_API_KEY", "do-ambiente"), ("REMOTEOCR_INSTANCES", "8"), ("REMOTEOCR_DEVICE", "cpu"), ("REMOTEOCR_NAME", "gpu-env"));

        var options = RemoteOcrOptions.Parse(["--host", "186.216.247.55:28747", "--api_key", "SEGREDO", "--instances", "4", "--device", "cuda"], env, currentDirectory: Path.GetTempPath());

        Assert.Equal(new Uri("ws://186.216.247.55:28747/ws/remote-ocr"), options.Host);
        Assert.Equal("SEGREDO", options.ApiKey);
        Assert.Equal(4, options.Instances);
        Assert.Equal("cuda", options.Device);
        Assert.Equal("gpu-env", options.Name);          // só no ambiente: vale
        Assert.Equal("auto", options.Engine);           // default
        Assert.Equal(1830, options.RequestTimeoutSeconds);
        Assert.Equal(180, options.StartupTimeoutSeconds);
        Assert.Equal(90, options.ShutdownGraceSeconds);
        Assert.Equal(Path.Combine("..", "tools", "convert_ocr"), options.ToolDir);
        Assert.Equal(Path.Combine("..", "storage"), options.Storage);
    }

    [Fact]
    public void So_ambiente_como_no_docker()
    {
        var env = Env(("REMOTEOCR_HOST", "workers.interno:8080"), ("REMOTEOCR_API_KEY", "k"), ("REMOTEOCR_INSTANCES", "2"), ("REMOTEOCR_DEVICE", "cuda"),
            ("REMOTEOCR_PYTHON", "/opt/venv/bin/python"), ("REMOTEOCR_TOOL_DIR", "/opt/tools/convert_ocr"), ("REMOTEOCR_STORAGE", "/data"), ("REMOTEOCR_ARGS", "--ocr-threads 2"));

        var options = RemoteOcrOptions.Parse([], env);

        Assert.Equal(new Uri("ws://workers.interno:8080/ws/remote-ocr"), options.Host);
        Assert.Equal("/opt/venv/bin/python", options.Python);
        Assert.Equal("/opt/tools/convert_ocr", options.ToolDir);
        Assert.Equal("/data", options.Storage);
        Assert.Equal("--ocr-threads 2", options.ExtraArgs);
        Assert.Equal("-m convert_ocr server --storage \"/data\" --engine auto --ocr-device cuda --ocr-threads 2", options.PythonArguments);
        Assert.Equal(Path.Combine("/data", "tmp"), options.TempRoot);
    }

    [Fact]
    public void Aceita_api_key_com_hifen_e_a_forma_chave_igual_valor()
    {
        var a = RemoteOcrOptions.Parse(["--host=h:1", "--api-key=abc", "--instances=3"], NoEnv);
        var b = RemoteOcrOptions.Parse(["--host", "h:1", "--api_key", "abc"], NoEnv);

        Assert.Equal("abc", a.ApiKey);
        Assert.Equal(3, a.Instances);
        Assert.Equal("abc", b.ApiKey);
        Assert.Equal(1, b.Instances);
    }

    [Theory]
    [InlineData("186.216.247.55:28747", "ws://186.216.247.55:28747/ws/remote-ocr")]
    [InlineData("ws://host:28747", "ws://host:28747/ws/remote-ocr")]
    [InlineData("ws://host:28747/", "ws://host:28747/ws/remote-ocr")]
    [InlineData("wss://ocr.exemplo.com.br", "wss://ocr.exemplo.com.br/ws/remote-ocr")]
    [InlineData("wss://ocr.exemplo.com.br/outro/path", "wss://ocr.exemplo.com.br/outro/path")]
    [InlineData("http://host:8080", "ws://host:8080/ws/remote-ocr")]
    [InlineData("https://host", "wss://host/ws/remote-ocr")]
    public void Normaliza_o_host(string input, string expected)
    {
        Assert.Equal(new Uri(expected), RemoteOcrOptions.NormalizeHost(input));
    }

    [Fact]
    public void Erros_de_configuracao_tem_mensagem_clara()
    {
        Assert.Contains("--host", Assert.Throws<OptionsException>(() => RemoteOcrOptions.Parse(["--api_key", "k"], NoEnv)).Message);
        Assert.Contains("--api_key", Assert.Throws<OptionsException>(() => RemoteOcrOptions.Parse(["--host", "h:1"], NoEnv)).Message);
        Assert.Contains("--device", Assert.Throws<OptionsException>(() => RemoteOcrOptions.Parse(["--host", "h:1", "--api_key", "k", "--device", "tpu"], NoEnv)).Message);
        Assert.Contains("--engine", Assert.Throws<OptionsException>(() => RemoteOcrOptions.Parse(["--host", "h:1", "--api_key", "k", "--engine", "x"], NoEnv)).Message);
        Assert.Contains("--instances", Assert.Throws<OptionsException>(() => RemoteOcrOptions.Parse(["--host", "h:1", "--api_key", "k", "--instances", "0"], NoEnv)).Message);
        Assert.Contains("inválido", Assert.Throws<OptionsException>(() => RemoteOcrOptions.Parse(["--host", "ftp://x", "--api_key", "k"], NoEnv)).Message);
        Assert.Contains("inesperado", Assert.Throws<OptionsException>(() => RemoteOcrOptions.Parse(["host"], NoEnv)).Message);

        var help = Assert.Throws<OptionsException>(() => RemoteOcrOptions.Parse(["--help"], NoEnv));
        Assert.True(help.Help);
        Assert.Contains("--api_key", help.Message);
    }

    [Fact]
    public void Python_padrao_e_o_do_venv_quando_existe()
    {
        var cwd = TestOptions.TempDirectory();
        Assert.Equal("python", RemoteOcrOptions.DefaultPython(cwd));

        var venvPython = Path.Combine(cwd, "..", "tools", ".venv", "bin", "python");
        Directory.CreateDirectory(Path.GetDirectoryName(venvPython)!);
        File.WriteAllText(venvPython, "");
        try
        {
            Assert.Equal(Path.Combine("..", "tools", ".venv", "bin", "python"), RemoteOcrOptions.DefaultPython(cwd));
        }
        finally
        {
            Directory.Delete(Path.GetFullPath(Path.Combine(cwd, "..", "tools")), recursive: true);
        }
    }
}
