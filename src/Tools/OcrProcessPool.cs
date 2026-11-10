using System.Diagnostics;
using System.Text.Json.Nodes;
using System.Threading.Channels;
using Microsoft.Extensions.Logging;

namespace RemoteOcr.Tools;

/// <summary>
/// Os N processos `convert_ocr server` deste cliente (um por instância anunciada ao Workers). Todos são iniciados na
/// subida (<see cref="StartAllAsync"/>, em paralelo: cada um carrega os modelos); um que não sobe fica "fora" (não conta
/// em <see cref="ReadyCount"/>) e é tentado de novo periodicamente. Um documento pega um processo livre
/// (<see cref="TryAcquire"/>) e o devolve no fim (<see cref="Release"/>). <see cref="ReadyCountChanged"/> avisa o
/// cliente para mandar `instances` ao Workers. Adaptado de Workers/Tools/ToolProcessPool.cs do repositório workers.
/// </summary>
public sealed class OcrProcessPool
{
    private readonly RemoteOcrOptions _options;
    private readonly ILogger _logger;
    private readonly List<OcrServerProcess> _all = [];
    private readonly Channel<OcrServerProcess> _idle = Channel.CreateUnbounded<OcrServerProcess>();
    private readonly object _lock = new();
    private readonly HashSet<int> _ready = [];   // terminaram a subida (ou subiram uma vez e reiniciam sozinhos no próximo pedido)
    private readonly HashSet<int> _down = [];    // não subiram; tentados de novo periodicamente
    private bool _started;

    /// <param name="options">Configuração do cliente (python, pasta, instâncias, timeouts).</param>
    /// <param name="logger">Log compartilhado pelos processos.</param>
    /// <param name="processFactory">Nos testes, troca o python por um processo falso (recebe o índice); null = System.Diagnostics.Process.</param>
    /// <param name="retryDownInterval">Intervalo entre tentativas de subir de novo um processo que falhou (padrão 5 min).</param>
    public OcrProcessPool(RemoteOcrOptions options, ILogger<OcrProcessPool> logger, Func<int, IChildProcess>? processFactory = null, TimeSpan? retryDownInterval = null)
    {
        _options = options;
        _logger = logger;
        RetryDownInterval = retryDownInterval ?? TimeSpan.FromMinutes(5);
        var processOptions = new OcrProcessOptions($"{options.Python} {options.PythonArguments}", options.StartupTimeoutSeconds, options.RequestTimeoutSeconds);
        for (var i = 0; i < Math.Max(1, options.Instances); i++)
        {
            var index = i;
            Func<IChildProcess> factory = processFactory is null
                ? () => new SystemChildProcess(BuildStartInfo(options))
                : () => processFactory(index);
            _all.Add(new OcrServerProcess(factory, processOptions, logger, index));
        }
    }

    public TimeSpan RetryDownInterval { get; }

    public IReadOnlyList<OcrServerProcess> Processes => _all;

    /// <summary>Processos configurados.</summary>
    public int Instances => _all.Count;

    /// <summary>
    /// Processos que terminaram a subida (ou que subiram uma vez e serão reiniciados no próximo pedido): a capacidade
    /// anunciada ao Workers. Enquanto um processo ainda carrega os modelos ele não conta.
    /// </summary>
    public int ReadyCount
    {
        get
        {
            lock (_lock)
            {
                return _ready.Count;
            }
        }
    }

    /// <summary>Processos livres neste momento.</summary>
    public int Free => _idle.Reader.Count;

    /// <summary>Evento `ready` de algum processo que já subiu (device, versão), para o hello.</summary>
    public JsonObject? AnyReady => _all.Select(p => p.Ready).FirstOrDefault(r => r is not null);

    /// <summary>Disparado com (capacidade, motivo) sempre que um processo termina de subir ou cai em definitivo — inclusive na subida inicial.</summary>
    public event Action<int, string>? ReadyCountChanged;

    /// <summary>
    /// Sobe todos os processos em paralelo e espera; depois fica tentando de novo os que falharam, a cada
    /// <see cref="RetryDownInterval"/>, até o token cancelar. Nunca lança: o resultado está em <see cref="ReadyCount"/>.
    /// </summary>
    public async Task StartAllAsync(CancellationToken cancellationToken)
    {
        if (_started)
        {
            return;
        }

        _started = true;
        _logger.LogInformation("subindo {Instances} processo(s) '{Python} {Args}' em '{Dir}' (até {Timeout}s cada)",
            _all.Count, _options.Python, _options.PythonArguments, _options.ToolDir, _options.StartupTimeoutSeconds);
        if (!OperatingSystem.IsWindows())
        {
            var nvidia = NvidiaLibraryDirectories(_options.Python);
            if (nvidia.Count > 0)
            {
                _logger.LogInformation("bibliotecas CUDA/cuDNN do venv no LD_LIBRARY_PATH dos processos: {Dirs}", string.Join(Path.PathSeparator, nvidia));
            }
        }

        await Task.WhenAll(_all.Select(p => StartOneAsync(p, initial: true, cancellationToken)));

        var ready = ReadyCount;
        if (ready == 0)
        {
            _logger.LogError("nenhum dos {Instances} processo(s) convert_ocr subiu; sem instâncias para anunciar (nova tentativa em {Minutes} min)", _all.Count, RetryDownInterval.TotalMinutes);
        }
        else if (ready < _all.Count)
        {
            _logger.LogWarning("{Ready} de {Instances} processo(s) convert_ocr prontos; os demais serão tentados de novo a cada {Minutes} min", ready, _all.Count, RetryDownInterval.TotalMinutes);
        }
        else
        {
            _logger.LogInformation("{Ready} processo(s) convert_ocr prontos", ready);
        }

        _ = RetryDownLoopAsync(cancellationToken);
    }

    private async Task StartOneAsync(OcrServerProcess process, bool initial, CancellationToken cancellationToken)
    {
        try
        {
            await process.StartAsync(cancellationToken);
            lock (_lock)
            {
                _down.Remove(process.Index);
                _ready.Add(process.Index);
            }

            _idle.Writer.TryWrite(process);
            ReadyCountChanged?.Invoke(ReadyCount, $"{process.Label} subiu");
        }
        catch (ToolException ex)
        {
            lock (_lock)
            {
                _ready.Remove(process.Index);
                _down.Add(process.Index);
            }

            _logger.LogError("{Label}: não subiu: {Error}", process.Label, ex.Detail);
            if (!initial)
            {
                ReadyCountChanged?.Invoke(ReadyCount, $"{process.Label} continua fora");
            }
        }
        catch (OperationCanceledException)
        {
            // desligamento.
        }
    }

    private async Task RetryDownLoopAsync(CancellationToken cancellationToken)
    {
        try
        {
            using var timer = new PeriodicTimer(RetryDownInterval);
            while (await timer.WaitForNextTickAsync(cancellationToken))
            {
                int[] down;
                lock (_lock)
                {
                    down = _down.ToArray();
                }

                foreach (var index in down)
                {
                    _logger.LogInformation("{Label}: tentando subir de novo", _all[index].Label);
                    await StartOneAsync(_all[index], initial: false, cancellationToken);
                }
            }
        }
        catch (OperationCanceledException)
        {
            // desligamento.
        }
    }

    /// <summary>Pega um processo livre sem esperar; false se todos estão ocupados ou fora.</summary>
    public bool TryAcquire(out OcrServerProcess? process) => _idle.Reader.TryRead(out process);

    /// <summary>
    /// Devolve o processo ao pool. Com <paramref name="failedToStart"/> (o pedido falhou com START ao reiniciá-lo), o
    /// processo fica fora até a próxima tentativa periódica e a capacidade cai.
    /// </summary>
    public void Release(OcrServerProcess process, bool failedToStart = false)
    {
        if (failedToStart)
        {
            lock (_lock)
            {
                _ready.Remove(process.Index);
                _down.Add(process.Index);
            }

            ReadyCountChanged?.Invoke(ReadyCount, $"{process.Label} não reiniciou");
            return;
        }

        _idle.Writer.TryWrite(process);
    }

    /// <summary>
    /// Estado de cada processo para o `health_result`: `status` da ferramenta (rss, documentos, erros, em voo) nos que
    /// estão rodando; `ready: false` nos demais.
    /// </summary>
    public async Task<JsonObject> HealthAsync(TimeSpan timeout)
    {
        var entries = await Task.WhenAll(_all.Select(async p =>
        {
            var entry = new JsonObject
            {
                ["index"] = p.Index,
                ["label"] = p.Label,
                ["pid"] = p.Pid,
                ["ready"] = p.IsRunning,
                ["busy"] = p.Busy,
                ["starts"] = p.Starts,
            };
            var status = p.IsRunning ? await p.SendControlAsync("status", timeout) : null;
            if (status is not null)
            {
                foreach (var key in new[] { "rss_mb", "max_rss_mb", "done", "errors", "timeouts", "ocr_pages", "cpu_ms", "uptime_s", "queued", "inflight", "inflight_pdf", "inflight_s" })
                {
                    if (status[key] is { } value)
                    {
                        entry[key] = value.DeepClone();
                    }
                }
            }
            else if (p.IsRunning)
            {
                entry["ready"] = false;
                entry["note"] = "sem resposta ao status";
            }

            return entry;
        }));

        var ready = entries.Count(e => (bool?)e["ready"] == true);
        return new JsonObject
        {
            ["ok"] = ready == _all.Count,
            ["instances_total"] = _all.Count,
            ["free"] = Free,
            ["in_flight"] = _all.Count(p => p.Busy),
            ["uptime_s"] = (long)(DateTimeOffset.UtcNow - Process.GetCurrentProcess().StartTime.ToUniversalTime()).TotalSeconds,
            ["instances"] = new JsonArray(entries.Cast<JsonNode?>().ToArray()),
        };
    }

    public async Task ShutdownAsync(TimeSpan grace)
    {
        await Task.WhenAll(_all.Select(p => p.ShutdownAsync(grace)));
    }

    // ------------------------------------------------------------------ linha de comando

    /// <summary>
    /// Monta o processo: python + `-m convert_ocr server --storage ... --engine ... --ocr-device ...` na pasta da
    /// ferramenta. Um python relativo com separador ("../tools/.venv/bin/python") é resolvido contra a pasta atual.
    /// No Linux as bibliotecas CUDA/cuDNN dos pacotes pip nvidia-* do venv entram no LD_LIBRARY_PATH do filho (fora
    /// do caminho padrão do loader; sem elas a sessão CUDA falha e o OCR cai para CPU).
    /// </summary>
    public static ProcessStartInfo BuildStartInfo(RemoteOcrOptions options)
    {
        var python = options.Python;
        if (!Path.IsPathRooted(python) && (python.Contains(Path.DirectorySeparatorChar) || python.Contains(Path.AltDirectorySeparatorChar)))
        {
            python = Path.GetFullPath(python);
        }

        var info = new ProcessStartInfo
        {
            FileName = python,
            Arguments = options.PythonArguments,
            WorkingDirectory = Directory.Exists(options.ToolDir) ? Path.GetFullPath(options.ToolDir) : Directory.GetCurrentDirectory(),
        };
        info.Environment["PYTHONIOENCODING"] = "utf-8";
        info.Environment["PYTHONUNBUFFERED"] = "1";
        info.Environment["NO_COLOR"] = "1";

        if (!OperatingSystem.IsWindows())
        {
            var nvidia = NvidiaLibraryDirectories(python);
            if (nvidia.Count > 0)
            {
                info.Environment.TryGetValue("LD_LIBRARY_PATH", out var current);
                var merged = nvidia.Where(dir => !PathListContains(current, dir)).ToList();
                if (merged.Count > 0)
                {
                    info.Environment["LD_LIBRARY_PATH"] = string.IsNullOrEmpty(current)
                        ? string.Join(Path.PathSeparator, merged)
                        : string.Join(Path.PathSeparator, merged) + Path.PathSeparator + current;
                }
            }
        }

        return info;
    }

    /// <summary>
    /// Pastas de bibliotecas nativas dos pacotes pip nvidia-* (CUDA runtime, cuBLAS, cuDNN, cuRAND...) no venv do Python
    /// informado: &lt;venv&gt;/lib/python3.*/site-packages/nvidia/*/lib que tenham algum .so. O venv é a pasta acima de bin/
    /// (ou Scripts/) quando tem pyvenv.cfg; para um Python sem separador ("python") vale VIRTUAL_ENV, se houver.
    /// </summary>
    public static IReadOnlyList<string> NvidiaLibraryDirectories(string pythonPath)
    {
        var venv = VenvRoot(pythonPath);
        if (venv is null)
        {
            return [];
        }

        var lib = Path.Combine(venv, "lib");
        if (!Directory.Exists(lib))
        {
            return [];
        }

        var result = new List<string>();
        foreach (var pythonDir in Directory.EnumerateDirectories(lib, "python3*").Order(StringComparer.Ordinal))
        {
            var nvidia = Path.Combine(pythonDir, "site-packages", "nvidia");
            if (!Directory.Exists(nvidia))
            {
                continue;
            }

            foreach (var package in Directory.EnumerateDirectories(nvidia).Order(StringComparer.Ordinal))
            {
                var packageLib = Path.Combine(package, "lib");
                if (Directory.Exists(packageLib) && Directory.EnumerateFiles(packageLib, "*.so*").Any())
                {
                    result.Add(packageLib);
                }
            }
        }

        return result;
    }

    private static string? VenvRoot(string pythonPath)
    {
        string? candidate = null;
        var hasSeparator = pythonPath.Contains(Path.DirectorySeparatorChar) || pythonPath.Contains(Path.AltDirectorySeparatorChar);
        if (hasSeparator)
        {
            var binDir = Path.GetDirectoryName(Path.GetFullPath(pythonPath));
            var binName = Path.GetFileName(binDir ?? "");
            if (binDir is not null && (binName.Equals("bin", StringComparison.OrdinalIgnoreCase) || binName.Equals("Scripts", StringComparison.OrdinalIgnoreCase)))
            {
                candidate = Path.GetDirectoryName(binDir);
            }
        }
        else
        {
            candidate = Environment.GetEnvironmentVariable("VIRTUAL_ENV");
        }

        return candidate is not null && File.Exists(Path.Combine(candidate, "pyvenv.cfg")) ? candidate : null;
    }

    private static bool PathListContains(string? pathList, string dir) =>
        !string.IsNullOrEmpty(pathList)
        && pathList.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries).Any(p => string.Equals(p.TrimEnd(Path.DirectorySeparatorChar), dir.TrimEnd(Path.DirectorySeparatorChar), StringComparison.Ordinal));
}
