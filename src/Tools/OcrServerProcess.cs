using System.Collections.Concurrent;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;

namespace RemoteOcr.Tools;

/// <summary>Como subir e esperar um processo convert_ocr (recorte do RemoteOcrOptions usado por esta camada).</summary>
public sealed record OcrProcessOptions(string CommandLine, int StartupTimeoutSeconds = 180, int RequestTimeoutSeconds = 1830);

/// <summary>
/// Um processo `python -m convert_ocr server` e a conversa NDJSON com ele por stdin/stdout (tools/convert_ocr/README.md,
/// "Protocolo do server-mode"): uma mensagem JSON por linha, um `convert` por vez, correlação pelo `id`. Adaptado de
/// Workers/Tools/ToolServerProcess.cs do repositório workers: aqui o processo é iniciado de propósito na subida do
/// cliente (<see cref="StartAsync"/>) e reiniciado no pedido seguinte se tiver morrido; `ping`/`status`
/// (<see cref="SendControlAsync"/>) passam por fora do gate de documentos, porque a thread leitora da ferramenta os
/// responde mesmo com um documento em voo — é o que alimenta o `health` do protocolo.
///
/// Reciclagem: a ferramenta pode avisar que vai encerrar de propósito entre documentos (memória acima de
/// `--max-rss-mb`) com <c>{"event":"exit","retry":true,...}</c> e sair com código 4. Um pedido escrito nesse intervalo
/// fica sem resposta e é reenviado uma vez ao processo novo (RECYCLE). Se o processo morre com um documento em voo,
/// o documento falha com CRASH e não é reenviado.
/// </summary>
public sealed class OcrServerProcess(Func<IChildProcess> processFactory, OcrProcessOptions options, ILogger logger, int index)
{
    private static readonly JsonSerializerOptions JsonOptions = new() { Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    private readonly SemaphoreSlim _gate = new(1, 1);          // um convert por vez
    private readonly SemaphoreSlim _writeLock = new(1, 1);     // uma linha por vez no stdin (convert e controle)
    private readonly ConcurrentDictionary<string, TaskCompletionSource<JsonObject>> _pending = new();
    private IChildProcess? _process;
    private TaskCompletionSource<JsonObject>? _ready;
    private Task _reader = Task.CompletedTask;
    private long _requestCounter;
    private volatile bool _retryPending;
    private volatile bool _busy;

    public int Index { get; } = index;

    public string Label { get; } = $"convert_ocr#{index + 1}";

    /// <summary>Quantas vezes um processo foi iniciado (diagnóstico e testes).</summary>
    public int Starts { get; private set; }

    public bool IsRunning => _process is { HasExited: false };

    /// <summary>Documento em voo neste processo.</summary>
    public bool Busy => _busy;

    /// <summary>Evento `ready` do processo atual (versão, device...), ou null se ainda não subiu.</summary>
    public JsonObject? Ready { get; private set; }

    public int? Pid => _process is { HasExited: false } p ? p.Id : null;

    /// <summary>Inicia o processo (se não estiver rodando) e espera o `ready`. Lança ToolException START.</summary>
    public async Task StartAsync(CancellationToken cancellationToken)
    {
        await _gate.WaitAsync(cancellationToken);
        try
        {
            await EnsureStartedAsync(cancellationToken);
        }
        finally
        {
            _gate.Release();
        }
    }

    /// <summary>
    /// Envia um `convert` (sem "id": é acrescentado aqui) e espera a resposta com o mesmo id. Um por vez; o processo é
    /// iniciado se preciso. Lança ToolException em START, CRASH e TIMEOUT; respostas "ok": false voltam como estão.
    /// </summary>
    public async Task<JsonObject> SendAsync(JsonObject request, CancellationToken cancellationToken)
    {
        await _gate.WaitAsync(cancellationToken);
        _busy = true;
        try
        {
            for (var attempt = 0; ; attempt++)
            {
                try
                {
                    return await SendOnceAsync(request, cancellationToken);
                }
                catch (ToolException ex) when (ex.Code == "RECYCLE" && attempt == 0)
                {
                    logger.LogInformation("{Label}: reenviando {Pdf} ao novo processo", Label, Path.GetFileName((string?)request["pdf"] ?? ""));
                }
            }
        }
        finally
        {
            _busy = false;
            _gate.Release();
        }
    }

    private async Task<JsonObject> SendOnceAsync(JsonObject request, CancellationToken cancellationToken)
    {
        await EnsureStartedAsync(cancellationToken);

        var id = NextId();
        request["id"] = id;

        var completion = new TaskCompletionSource<JsonObject>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pending[id] = completion;
        try
        {
            await WriteAsync(request, cancellationToken);

            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            timeout.CancelAfter(TimeSpan.FromSeconds(Math.Max(1, options.RequestTimeoutSeconds)));
            try
            {
                return await completion.Task.WaitAsync(timeout.Token);
            }
            catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
            {
                logger.LogError("{Label}: sem resposta em {Seconds}s para {Pdf}; encerrando o processo", Label, options.RequestTimeoutSeconds, Path.GetFileName((string?)request["pdf"] ?? ""));
                Kill();
                throw new ToolException("TIMEOUT", $"convert_ocr não respondeu em {options.RequestTimeoutSeconds}s; processo encerrado");
            }
        }
        finally
        {
            _pending.TryRemove(id, out _);
        }
    }

    /// <summary>
    /// `ping` ou `status` fora do gate de documentos (a ferramenta responde na thread leitora, mesmo ocupada). Null se o
    /// processo não está rodando ou não respondeu no prazo.
    /// </summary>
    public async Task<JsonObject?> SendControlAsync(string command, TimeSpan timeout)
    {
        if (!IsRunning || _retryPending)
        {
            return null;
        }

        var id = NextId();
        var completion = new TaskCompletionSource<JsonObject>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pending[id] = completion;
        try
        {
            await WriteAsync(new JsonObject { ["cmd"] = command, ["id"] = id }, CancellationToken.None);
            return await completion.Task.WaitAsync(timeout);
        }
        catch (Exception ex) when (ex is ToolException or TimeoutException or IOException)
        {
            return null;
        }
        finally
        {
            _pending.TryRemove(id, out _);
        }
    }

    /// <summary>Mata o processo (cancel com kill do Workers / timeout). O pedido em voo falha com CRASH; o próximo pedido reinicia.</summary>
    public void Kill()
    {
        try
        {
            _process?.Kill();
        }
        catch (Exception)
        {
            // já morreu.
        }
    }

    /// <summary>Encerra o processo de forma ordenada (`shutdown` + EOF em stdin); força se não sair a tempo.</summary>
    public async Task ShutdownAsync(TimeSpan grace)
    {
        var process = _process;
        if (process is null)
        {
            return;
        }

        try
        {
            if (!process.HasExited)
            {
                await WriteAsync(new JsonObject { ["cmd"] = "shutdown" }, CancellationToken.None);
                process.StandardInput.Close();
                using var cts = new CancellationTokenSource(grace);
                try
                {
                    await process.WaitForExitAsync(cts.Token);
                }
                catch (OperationCanceledException)
                {
                    logger.LogWarning("{Label}: processo não encerrou em {Grace}s; matando", Label, grace.TotalSeconds);
                    process.Kill();
                }
            }
        }
        catch (Exception ex) when (ex is IOException or ObjectDisposedException or InvalidOperationException or ToolException)
        {
            // stdin já fechado / processo já morto: nada a fazer.
        }
        finally
        {
            await _reader.ConfigureAwait(ConfigureAwaitOptions.SuppressThrowing);
            process.Dispose();
            _process = null;
        }
    }

    /// <summary>
    /// Campos do evento `ready` além de event/version/pid/storage, achatados um nível, para a linha de log da subida:
    /// "engine=auto device.kind=cuda device.active=CUDAExecutionProvider ...".
    /// </summary>
    public static string ReadyDetails(JsonObject ready)
    {
        var parts = new List<string>();
        foreach (var (key, value) in ready)
        {
            if (key is "event" or "version" or "pid" or "storage")
            {
                continue;
            }

            if (value is JsonObject nested)
            {
                parts.AddRange(nested.Where(pair => pair.Value is JsonValue).Select(pair => $"{key}.{pair.Key}={pair.Value}"));
            }
            else if (value is JsonValue)
            {
                parts.Add($"{key}={value}");
            }
        }

        return string.Join(" ", parts);
    }

    // ------------------------------------------------------------------ ciclo de vida

    private string NextId() => $"{Environment.ProcessId}-{Index}-{Interlocked.Increment(ref _requestCounter)}";

    private async Task EnsureStartedAsync(CancellationToken cancellationToken)
    {
        if (_process is { HasExited: false })
        {
            if (!_retryPending)
            {
                return;
            }

            // o processo avisou que vai sair: espera-o em vez de escrever num stdin que está fechando
            try
            {
                using var exitWait = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
                exitWait.CancelAfter(TimeSpan.FromSeconds(10));
                await _process.WaitForExitAsync(exitWait.Token);
            }
            catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
            {
                logger.LogWarning("{Label}: processo avisou que ia encerrar e não saiu em 10s; matando", Label);
                Kill();
            }
        }

        if (_process is not null)
        {
            await _reader.ConfigureAwait(ConfigureAwaitOptions.SuppressThrowing);
            _process.Dispose();
            _process = null;
        }

        _retryPending = false;
        Ready = null;
        var process = processFactory();
        _ready = new TaskCompletionSource<JsonObject>(TaskCreationOptions.RunContinuationsAsynchronously);
        try
        {
            process.Start();
        }
        catch (Exception ex)
        {
            process.Dispose();
            throw new ToolException("START", $"não foi possível iniciar '{options.CommandLine}': {ex.Message}");
        }

        Starts++;
        _process = process;
        _ = PumpStderrAsync(process);
        _reader = ReadStdoutAsync(process);

        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(TimeSpan.FromSeconds(Math.Max(1, options.StartupTimeoutSeconds)));
        try
        {
            var ready = await _ready.Task.WaitAsync(timeout.Token);
            Ready = ready;
            logger.LogInformation("{Label}: convert_ocr {Version} pronto (pid {Pid}): {Details}", Label, (string?)ready["version"], (int?)ready["pid"], ReadyDetails(ready));
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            Kill();
            throw new ToolException("START", $"convert_ocr não ficou pronto em {options.StartupTimeoutSeconds}s ('{options.CommandLine}')");
        }
    }

    private async Task WriteAsync(JsonObject message, CancellationToken cancellationToken)
    {
        var process = _process ?? throw new ToolException("CRASH", "processo convert_ocr não está em execução");
        await _writeLock.WaitAsync(cancellationToken);
        try
        {
            await process.StandardInput.WriteAsync((message.ToJsonString(JsonOptions) + "\n").AsMemory(), cancellationToken);
            await process.StandardInput.FlushAsync(cancellationToken);
        }
        catch (Exception ex) when (ex is IOException or ObjectDisposedException)
        {
            if (_retryPending)
            {
                throw new ToolException("RECYCLE", "processo convert_ocr encerrou para reinício antes de receber o pedido");
            }

            throw new ToolException("CRASH", $"falha ao escrever no stdin do convert_ocr: {ex.Message}");
        }
        finally
        {
            _writeLock.Release();
        }
    }

    private async Task ReadStdoutAsync(IChildProcess process)
    {
        try
        {
            while (await process.StandardOutput.ReadLineAsync() is { } line)
            {
                if (string.IsNullOrWhiteSpace(line))
                {
                    continue;
                }

                JsonObject? message;
                try
                {
                    message = JsonNode.Parse(line) as JsonObject;
                }
                catch (JsonException)
                {
                    message = null;
                }

                if (message is null)
                {
                    logger.LogDebug("{Label} stdout: {Line}", Label, line);
                    continue;
                }

                Dispatch(message);
            }
        }
        catch (Exception ex) when (ex is IOException or ObjectDisposedException)
        {
            // pipe fechado: tratado como fim do processo abaixo.
        }

        var exitCode = await ExitCodeAsync(process);
        var reason = exitCode is null ? "stdout fechado" : $"exit code {exitCode}";
        var recycled = _retryPending;
        var failure = recycled
            ? new ToolException("RECYCLE", $"processo convert_ocr encerrou para reinício ({reason}) sem tratar o pedido")
            : new ToolException("CRASH", $"processo convert_ocr encerrou durante o pedido ({reason})");
        _ready?.TrySetException(new ToolException("START", $"convert_ocr encerrou antes de ficar pronto ({reason}); confira o python, o pacote e o stderr em Debug"));
        foreach (var (id, completion) in _pending)
        {
            if (!completion.TrySetException(failure))
            {
                continue;
            }

            if (recycled)
            {
                logger.LogInformation("{Label}: processo reciclado ({Reason}) com o pedido {Id} à espera; será reenviado", Label, reason, id);
            }
            else
            {
                logger.LogError("{Label}: processo encerrou ({Reason}) com o pedido {Id} em voo", Label, reason, id);
            }
        }

        if (_pending.IsEmpty)
        {
            if (recycled)
            {
                logger.LogInformation("{Label}: processo reciclado ({Reason})", Label, reason);
            }
            else
            {
                logger.LogWarning("{Label}: processo encerrou ({Reason})", Label, reason);
            }
        }
    }

    private void Dispatch(JsonObject message)
    {
        if (message["event"] is JsonValue eventValue)
        {
            var eventName = eventValue.ToString();
            if (eventName == "ready")
            {
                _ready?.TrySetResult(message);
            }
            else if (eventName == "exit" && (bool?)message["retry"] == true)
            {
                _retryPending = true;
                logger.LogInformation("{Label}: vai encerrar para reinício ({Reason}; rss {Rss} MB, limite {Limit} MB, {Documents} documentos)",
                    Label, (string?)message["reason"], (int?)message["rss_mb"], (int?)message["limit_mb"], (int?)message["documents"]);
            }
            else
            {
                logger.LogWarning("{Label}: evento {Event}: {Message}", Label, eventName, message.ToJsonString());
            }
            return;
        }

        var id = message["id"]?.ToString();
        if (id is not null && _pending.TryGetValue(id, out var completion))
        {
            completion.TrySetResult(message);
        }
        else
        {
            logger.LogDebug("{Label}: resposta sem pedido correspondente: {Message}", Label, message.ToJsonString());
        }
    }

    private async Task PumpStderrAsync(IChildProcess process)
    {
        try
        {
            while (await process.StandardError.ReadLineAsync() is { } line)
            {
                if (line.Length > 0)
                {
                    logger.LogDebug("{Label} stderr: {Line}", Label, line);
                }
            }
        }
        catch (Exception ex) when (ex is IOException or ObjectDisposedException)
        {
            // processo encerrado.
        }
    }

    private static async Task<int?> ExitCodeAsync(IChildProcess process)
    {
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            await process.WaitForExitAsync(cts.Token);
            return process.ExitCode;
        }
        catch (Exception)
        {
            return null;
        }
    }
}
