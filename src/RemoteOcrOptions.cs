using RemoteOcr.Protocol;

namespace RemoteOcr;

/// <summary>
/// Configuração do cliente remote-ocr: linha de comando (`--host 1.2.3.4:28747 --api_key X --instances 4 --device cuda`)
/// com fallback nas variáveis de ambiente `REMOTEOCR_*` (o docker usa só as variáveis) e depois nos defaults. Caminhos
/// relativos (python, pasta da ferramenta, storage) valem a partir da pasta atual, como no Workers.
/// </summary>
public sealed record RemoteOcrOptions
{
    public const string EnvPrefix = "REMOTEOCR_";

    /// <summary>URL completa do WebSocket do Workers (ws://host:porta/ws/remote-ocr).</summary>
    public required Uri Host { get; init; }

    /// <summary>Chave única dos clientes (Security:ApiKey do Workers), enviada no header X-Api-Key.</summary>
    public required string ApiKey { get; init; }

    /// <summary>Processos `convert_ocr server` = instâncias anunciadas ao Workers.</summary>
    public int Instances { get; init; } = 1;

    /// <summary>auto | cpu | cuda | dml → `--ocr-device` da ferramenta.</summary>
    public string Device { get; init; } = "auto";

    /// <summary>auto | rapidocr | pymupdf4llm → `--engine` da ferramenta.</summary>
    public string Engine { get; init; } = "auto";

    /// <summary>Executável do Python (venv com o convert_ocr instalado).</summary>
    public required string Python { get; init; }

    /// <summary>Pasta de trabalho dos processos (tools/convert_ocr).</summary>
    public required string ToolDir { get; init; }

    /// <summary>`--storage` da ferramenta (logs em &lt;storage&gt;/logs) e pasta dos arquivos temporários (&lt;storage&gt;/tmp/&lt;id&gt;).</summary>
    public required string Storage { get; init; }

    /// <summary>Parâmetros extras anexados à linha do Python (`--ocr-threads 2 --max-rss-mb 4096 --timeout 1800`).</summary>
    public string ExtraArgs { get; init; } = "";

    /// <summary>Nome deste cliente nos logs do Workers (padrão: nome da máquina).</summary>
    public required string Name { get; init; }

    /// <summary>Tempo para cada processo emitir o `ready` (carrega os modelos e aquece a GPU).</summary>
    public int StartupTimeoutSeconds { get; init; } = 180;

    /// <summary>Limite por documento do lado .NET: `--timeout` da ferramenta (1800) + 30 s do watchdog dela.</summary>
    public int RequestTimeoutSeconds { get; init; } = 1830;

    /// <summary>No desligamento (SIGTERM), quanto esperar os documentos em voo terminarem antes de fechar.</summary>
    public int ShutdownGraceSeconds { get; init; } = 90;

    /// <summary>Linha de argumentos do Python: `-m convert_ocr server --storage ... --engine ... --ocr-device ... &lt;extras&gt;`.</summary>
    public string PythonArguments =>
        $"-m convert_ocr server --storage \"{Path.GetFullPath(Storage)}\" --engine {Engine} --ocr-device {Device}" + (string.IsNullOrWhiteSpace(ExtraArgs) ? "" : " " + ExtraArgs.Trim());

    /// <summary>Pasta dos arquivos temporários de cada pedido.</summary>
    public string TempRoot => Path.Combine(Path.GetFullPath(Storage), "tmp");

    public static readonly string Usage = """
        remote-ocr: cliente de OCR do licitagpt. Conecta ao Workers por WebSocket, sobe N processos convert_ocr e atende
        os documentos que o Workers mandar.

        uso: RemoteOcr --host HOST:PORTA --api_key CHAVE [opções]

          --host HOST[:PORTA] | ws://... | wss://...   servidor Workers (path padrão /ws/remote-ocr)   REMOTEOCR_HOST
          --api_key CHAVE                               Security:ApiKey do Workers                   REMOTEOCR_API_KEY
          --instances N                                 processos Python (padrão 1)                  REMOTEOCR_INSTANCES
          --device auto|cpu|cuda|dml                    --ocr-device da ferramenta (padrão auto)     REMOTEOCR_DEVICE
          --engine auto|rapidocr|pymupdf4llm            --engine da ferramenta (padrão auto)         REMOTEOCR_ENGINE
          --python CAMINHO                              python do venv (padrão ../tools/.venv)       REMOTEOCR_PYTHON
          --tool-dir PASTA                              tools/convert_ocr (padrão ../tools/convert_ocr) REMOTEOCR_TOOL_DIR
          --storage PASTA                               logs e temporários (padrão ../storage)       REMOTEOCR_STORAGE
          --args "..."                                  extras para a ferramenta                     REMOTEOCR_ARGS
          --name NOME                                   nome do cliente no log do Workers            REMOTEOCR_NAME
          --startup-timeout S                           ready dos processos (padrão 180)             REMOTEOCR_STARTUP_TIMEOUT
          --request-timeout S                           por documento (padrão 1830)                  REMOTEOCR_REQUEST_TIMEOUT
          --shutdown-grace S                            espera no desligamento (padrão 90)           REMOTEOCR_SHUTDOWN_GRACE
          --help

        As opções da linha de comando têm precedência sobre as variáveis de ambiente.
        """;

    /// <summary>Lê a configuração de <paramref name="args"/> e <paramref name="env"/>; lança OptionsException com a mensagem de uso em caso de erro.</summary>
    public static RemoteOcrOptions Parse(string[] args, Func<string, string?> env, string? currentDirectory = null)
    {
        var cli = ParseArgs(args);
        if (cli.ContainsKey("help"))
        {
            throw new OptionsException(Usage, help: true);
        }

        string? Get(string cliName, string envName) =>
            cli.TryGetValue(cliName, out var v) ? v : env(EnvPrefix + envName) is { Length: > 0 } e ? e : null;

        var host = Get("host", "HOST") ?? throw new OptionsException("--host (ou REMOTEOCR_HOST) é obrigatório.");
        var apiKey = Get("api_key", "API_KEY") ?? Get("api-key", "API_KEY") ?? throw new OptionsException("--api_key (ou REMOTEOCR_API_KEY) é obrigatório.");
        var device = (Get("device", "DEVICE") ?? "auto").ToLowerInvariant();
        if (device is not ("auto" or "cpu" or "cuda" or "dml"))
        {
            throw new OptionsException($"--device deve ser auto|cpu|cuda|dml, recebido '{device}'.");
        }

        var engine = (Get("engine", "ENGINE") ?? "auto").ToLowerInvariant();
        if (engine is not ("auto" or "rapidocr" or "pymupdf4llm"))
        {
            throw new OptionsException($"--engine deve ser auto|rapidocr|pymupdf4llm, recebido '{engine}'.");
        }

        var cwd = currentDirectory ?? Directory.GetCurrentDirectory();
        return new RemoteOcrOptions
        {
            Host = NormalizeHost(host),
            ApiKey = apiKey,
            Instances = PositiveInt(Get("instances", "INSTANCES"), "--instances", 1),
            Device = device,
            Engine = engine,
            Python = Get("python", "PYTHON") ?? DefaultPython(cwd),
            ToolDir = Get("tool-dir", "TOOL_DIR") ?? Get("tool_dir", "TOOL_DIR") ?? Path.Combine("..", "tools", "convert_ocr"),
            Storage = Get("storage", "STORAGE") ?? Path.Combine("..", "storage"),
            ExtraArgs = Get("args", "ARGS") ?? "",
            Name = Get("name", "NAME") ?? Environment.MachineName,
            StartupTimeoutSeconds = PositiveInt(Get("startup-timeout", "STARTUP_TIMEOUT") ?? Get("startup_timeout", "STARTUP_TIMEOUT"), "--startup-timeout", 180),
            RequestTimeoutSeconds = PositiveInt(Get("request-timeout", "REQUEST_TIMEOUT") ?? Get("request_timeout", "REQUEST_TIMEOUT"), "--request-timeout", 1830),
            ShutdownGraceSeconds = PositiveInt(Get("shutdown-grace", "SHUTDOWN_GRACE") ?? Get("shutdown_grace", "SHUTDOWN_GRACE"), "--shutdown-grace", 90),
        };
    }

    /// <summary>`--chave valor`, `--chave=valor` e `--flag` (sem valor) viram um dicionário; a chave perde os "--".</summary>
    public static Dictionary<string, string> ParseArgs(string[] args)
    {
        var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        for (var i = 0; i < args.Length; i++)
        {
            var arg = args[i];
            if (!arg.StartsWith("--", StringComparison.Ordinal))
            {
                throw new OptionsException($"argumento inesperado '{arg}'.");
            }

            var body = arg[2..];
            var eq = body.IndexOf('=');
            if (eq >= 0)
            {
                result[body[..eq]] = body[(eq + 1)..];
            }
            else if (i + 1 < args.Length && !args[i + 1].StartsWith("--", StringComparison.Ordinal))
            {
                result[body] = args[++i];
            }
            else
            {
                result[body] = "";
            }
        }

        return result;
    }

    /// <summary>
    /// "host:porta" → ws://host:porta/ws/remote-ocr; "ws://..." e "wss://..." ficam (ganham o path padrão se não tiverem);
    /// "http(s)://" vira ws(s)://.
    /// </summary>
    public static Uri NormalizeHost(string host)
    {
        var text = host.Trim();
        if (!text.Contains("://", StringComparison.Ordinal))
        {
            text = "ws://" + text;
        }

        text = text.Replace("http://", "ws://", StringComparison.OrdinalIgnoreCase).Replace("https://", "wss://", StringComparison.OrdinalIgnoreCase);
        if (!Uri.TryCreate(text, UriKind.Absolute, out var uri) || uri.Scheme is not ("ws" or "wss"))
        {
            throw new OptionsException($"--host inválido: '{host}' (use host:porta, ws://host:porta ou wss://host:porta).");
        }

        if (uri.AbsolutePath is "" or "/")
        {
            uri = new UriBuilder(uri) { Path = RemoteOcrProtocol.Path }.Uri;
        }

        return uri;
    }

    private static int PositiveInt(string? value, string name, int fallback)
    {
        if (value is null)
        {
            return fallback;
        }

        if (!int.TryParse(value, out var n) || n < 1)
        {
            throw new OptionsException($"{name} deve ser um inteiro >= 1, recebido '{value}'.");
        }

        return n;
    }

    /// <summary>python do venv das ferramentas (../tools/.venv) quando existe; senão "python" do PATH.</summary>
    public static string DefaultPython(string cwd)
    {
        foreach (var candidate in new[]
                 {
                     Path.Combine("..", "tools", ".venv", "bin", "python"),
                     Path.Combine("..", "tools", ".venv", "Scripts", "python.exe"),
                     Path.Combine("tools", ".venv", "bin", "python"),
                     Path.Combine("tools", ".venv", "Scripts", "python.exe"),
                 })
        {
            if (File.Exists(Path.Combine(cwd, candidate)))
            {
                return candidate;
            }
        }

        return "python";
    }
}

/// <summary>Erro de configuração: a mensagem vai para o stderr e o processo sai com código 2 (0 para --help).</summary>
public sealed class OptionsException(string message, bool help = false) : Exception(message)
{
    public bool Help { get; } = help;
}
