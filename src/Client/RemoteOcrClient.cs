using System.Collections.Concurrent;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Reflection;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using RemoteOcr.Protocol;
using RemoteOcr.Tools;

namespace RemoteOcr.Client;

/// <summary>
/// O cliente remote-ocr: conecta ao Workers (ws://host:porta/ws/remote-ocr, header X-Api-Key), manda `hello` com as
/// instâncias (processos Python prontos) e fica recebendo `convert` (binário: cabeçalho + PDF, gravado direto em
/// &lt;storage&gt;/tmp/&lt;id&gt;/), que distribui pelos processos livres do <see cref="OcrProcessPool"/>; cada um vira um `result`.
/// Também responde `health` (estado dos processos), `cancel` (kill ou descarte) e `ping`, e avisa `instances` quando a
/// quantidade de processos prontos muda. Se a conexão cai, reconecta com backoff exponencial (1 s → 30 s; chave
/// recusada: 60 s); documentos em voo terminam e o resultado é descartado — o Workers já devolveu o item à fila.
/// No desligamento (SIGTERM/Ctrl+C) manda `bye`, espera os documentos em voo (ShutdownGraceSeconds) e fecha.
/// </summary>
public sealed class RemoteOcrClient(RemoteOcrOptions options, OcrProcessPool pool, IHostApplicationLifetime lifetime, ILogger<RemoteOcrClient> logger) : BackgroundService
{
    private static readonly string Version = Assembly.GetExecutingAssembly().GetName().Version?.ToString(3) ?? "0.0.0";

    private readonly ConcurrentDictionary<string, InFlight> _inFlight = new();
    private volatile Connection? _connection;
    private volatile bool _stopping;

    /// <summary>Conexões bem-sucedidas (diagnóstico/testes).</summary>
    public int Connections { get; private set; }

    public bool IsConnected => _connection is { Socket.State: WebSocketState.Open };

    public int InFlightCount => _inFlight.Count;

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        var poolStart = pool.StartAllAsync(stoppingToken);
        _ = poolStart.ContinueWith(_ =>
        {
            if (pool.ReadyCount == 0 && !stoppingToken.IsCancellationRequested)
            {
                logger.LogCritical("nenhum processo convert_ocr subiu; encerrando com código 3 para o docker reiniciar o container");
                Environment.ExitCode = 3;
                lifetime.StopApplication();
            }
        }, TaskScheduler.Default);
        pool.ReadyCountChanged += OnReadyCountChanged;

        var backoff = TimeSpan.FromSeconds(1);
        while (!stoppingToken.IsCancellationRequested)
        {
            var unauthorized = false;
            try
            {
                await RunConnectionAsync(stoppingToken);
                backoff = TimeSpan.FromSeconds(1);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (WebSocketException ex) when (ex.Message.Contains("401", StringComparison.Ordinal) || ex.Message.Contains("Unauthorized", StringComparison.OrdinalIgnoreCase))
            {
                unauthorized = true;
                logger.LogError("chave de API recusada pelo servidor {Host} (401); confira --api_key / REMOTEOCR_API_KEY", options.Host);
            }
            catch (Exception ex) when (ex is WebSocketException or IOException or HttpRequestException or TimeoutException or SocketException or InvalidDataException)
            {
                logger.LogWarning("conexão com {Host} falhou: {Error}", options.Host, ex.Message);
            }
            finally
            {
                _connection = null;
            }

            if (stoppingToken.IsCancellationRequested)
            {
                break;
            }

            var delay = unauthorized ? TimeSpan.FromSeconds(60) : backoff + TimeSpan.FromMilliseconds(Random.Shared.Next(0, 500));
            logger.LogInformation("reconectando em {Seconds}s", (int)delay.TotalSeconds);
            try
            {
                await Task.Delay(delay, stoppingToken);
            }
            catch (OperationCanceledException)
            {
                break;
            }

            backoff = TimeSpan.FromSeconds(Math.Min(30, backoff.TotalSeconds * 2));
        }

        pool.ReadyCountChanged -= OnReadyCountChanged;
    }

    // ------------------------------------------------------------------ conexão

    private async Task RunConnectionAsync(CancellationToken stoppingToken)
    {
        using var socket = new ClientWebSocket();
        socket.Options.SetRequestHeader(RemoteOcrProtocol.ApiKeyHeader, options.ApiKey);
        socket.Options.KeepAliveInterval = TimeSpan.FromSeconds(20);
        socket.Options.KeepAliveTimeout = TimeSpan.FromSeconds(60);

        using (var connectTimeout = CancellationTokenSource.CreateLinkedTokenSource(stoppingToken))
        {
            connectTimeout.CancelAfter(TimeSpan.FromSeconds(15));
            try
            {
                await socket.ConnectAsync(options.Host, connectTimeout.Token);
            }
            catch (OperationCanceledException) when (!stoppingToken.IsCancellationRequested)
            {
                throw new TimeoutException("o servidor não respondeu ao connect em 15s");
            }
        }

        var connection = new Connection(socket);
        var hello = BuildHello();
        var announced = (int)hello["instances"]!;
        await connection.SendJsonAsync(hello, stoppingToken);

        var first = await connection.ReceiveTextAsync(TimeSpan.FromSeconds(10), stoppingToken)
            ?? throw new InvalidDataException("o servidor fechou a conexão antes do hello_ok");
        switch ((string?)first["type"])
        {
            case RemoteOcrProtocol.Types.HelloOk:
                break;
            case RemoteOcrProtocol.Types.Reject:
                throw new InvalidDataException($"servidor recusou o hello: {(string?)first["code"]} {(string?)first["message"]}");
            default:
                throw new InvalidDataException($"esperado hello_ok, veio '{(string?)first["type"]}'");
        }

        _connection = connection;
        Connections++;
        logger.LogInformation("conectado a {Host} como '{Name}': {Instances} instância(s) anunciada(s); servidor aceita PDF até {MaxPdf} MB, timeout {Timeout}s, {Total} instância(s) no total",
            options.Host, options.Name, pool.ReadyCount, (int?)first["max_pdf_mb"], (int?)first["request_timeout_s"], (int?)first["total_instances"]);

        // Processos que subiram (ou caíram) entre o hello e o hello_ok: o aviso teria se perdido sem conexão.
        if (pool.ReadyCount != announced)
        {
            await connection.TrySendJsonAsync(InstancesMessage(pool.ReadyCount, "sincronização"));
        }

        var reason = await ReceiveLoopAsync(connection, stoppingToken);
        if (_stopping)
        {
            logger.LogInformation("conexão encerrada ({Reason})", reason);
        }
        else
        {
            logger.LogWarning("conexão com o servidor caiu ({Reason}); {InFlight} documento(s) em voo serão descartados", reason, _inFlight.Count);
        }
    }

    private JsonObject BuildHello()
    {
        var ready = pool.AnyReady;
        return new JsonObject
        {
            ["type"] = RemoteOcrProtocol.Types.Hello,
            ["protocol"] = RemoteOcrProtocol.Version,
            ["name"] = options.Name,
            ["version"] = Version,
            ["instances"] = pool.ReadyCount,
            ["instances_total"] = pool.Instances,
            ["device"] = ready?["device"]?.DeepClone() ?? new JsonObject { ["kind"] = options.Device },
            ["tool_version"] = (string?)ready?["version"],
            ["engine"] = (string?)ready?["engine"] ?? options.Engine,
            ["pymupdf"] = (string?)ready?["pymupdf"],
            ["hostname"] = Environment.MachineName,
            ["pid"] = Environment.ProcessId,
        };
    }

    /// <summary>Mudança de capacidade; leva o device e a versão da ferramenta, que o hello pode não ter tido (processos ainda carregando).</summary>
    private JsonObject InstancesMessage(int instances, string reason)
    {
        var message = new JsonObject
        {
            ["type"] = RemoteOcrProtocol.Types.Instances,
            ["instances"] = instances,
            ["reason"] = reason,
        };
        if (pool.AnyReady is { } ready)
        {
            message["device"] = ready["device"]?.DeepClone();
            message["tool_version"] = (string?)ready["version"];
            message["engine"] = (string?)ready["engine"];
        }

        return message;
    }

    private void OnReadyCountChanged(int instances, string reason)
    {
        logger.LogInformation("instâncias: {Instances} ({Reason})", instances, reason);
        if (_connection is { } connection)
        {
            _ = connection.TrySendJsonAsync(InstancesMessage(instances, reason));
        }
    }

    // ------------------------------------------------------------------ recepção

    /// <summary>Lê mensagens até o socket fechar; devolve o motivo.</summary>
    private async Task<string> ReceiveLoopAsync(Connection connection, CancellationToken stoppingToken)
    {
        var buffer = new byte[64 * 1024];
        try
        {
            while (true)
            {
                var result = await connection.Socket.ReceiveAsync(buffer, stoppingToken);
                if (result.MessageType == WebSocketMessageType.Close)
                {
                    await connection.CloseOutputQuietlyAsync(WebSocketCloseStatus.NormalClosure, "bye");
                    return $"fechado pelo servidor: {connection.Socket.CloseStatus} {connection.Socket.CloseStatusDescription}".TrimEnd();
                }

                if (result.MessageType == WebSocketMessageType.Text)
                {
                    var payload = await connection.ReceiveRestAsync(buffer, result, stoppingToken);
                    var message = RemoteOcrProtocol.ParseText(payload);
                    if (message is null)
                    {
                        logger.LogDebug("mensagem de texto inválida ignorada");
                        continue;
                    }

                    await HandleControlAsync(connection, message, stoppingToken);
                }
                else
                {
                    await ReceiveConvertAsync(connection, buffer, result, stoppingToken);
                }
            }
        }
        catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
        {
            return "desligamento";
        }
        catch (WebSocketException ex)
        {
            return $"conexão perdida: {ex.Message}";
        }
        catch (Exception ex) when (ex is IOException or ObjectDisposedException or InvalidOperationException)
        {
            return $"conexão perdida: {ex.Message}";
        }
    }

    private async Task HandleControlAsync(Connection connection, JsonObject message, CancellationToken stoppingToken)
    {
        switch ((string?)message["type"])
        {
            case RemoteOcrProtocol.Types.Cancel:
            {
                var id = (string?)message["id"] ?? "";
                var kill = (bool?)message["kill"] ?? false;
                if (_inFlight.TryGetValue(id, out var inFlight))
                {
                    inFlight.Discard = true;
                    if (kill)
                    {
                        logger.LogWarning("cancel com kill de {Pdf} ({Id}): matando {Label}", inFlight.PdfName, id, inFlight.Process?.Label);
                        inFlight.Process?.Kill();
                    }
                    else
                    {
                        logger.LogInformation("cancel de {Pdf} ({Id}): o resultado será descartado", inFlight.PdfName, id);
                    }
                }
                else
                {
                    logger.LogDebug("cancel de pedido desconhecido {Id}", id);
                }
                break;
            }

            case RemoteOcrProtocol.Types.Health:
            {
                var health = await pool.HealthAsync(TimeSpan.FromSeconds(10));
                health["type"] = RemoteOcrProtocol.Types.HealthResult;
                health["id"] = message["id"]?.DeepClone();
                health["version"] = Version;
                await connection.TrySendJsonAsync(health);
                break;
            }

            case RemoteOcrProtocol.Types.Ping:
                await connection.TrySendJsonAsync(new JsonObject { ["type"] = RemoteOcrProtocol.Types.Pong, ["ts"] = message["ts"]?.DeepClone() });
                break;

            case RemoteOcrProtocol.Types.Pong:
                break;

            case RemoteOcrProtocol.Types.Bye:
                logger.LogInformation("servidor avisou que vai desligar ({Reason}); reconectando quando voltar", (string?)message["reason"]);
                break;

            case RemoteOcrProtocol.Types.HelloOk:
            case RemoteOcrProtocol.Types.Reject:
                break;

            default:
                logger.LogDebug("mensagem '{Type}' desconhecida ignorada", (string?)message["type"]);
                break;
        }
    }

    /// <summary>
    /// Mensagem binária `convert`: junta bytes até decodificar o cabeçalho, grava o resto (e os frames seguintes) direto
    /// no PDF em &lt;storage&gt;/tmp/&lt;id&gt;/ e, no fim da mensagem, despacha o documento num Task próprio.
    /// </summary>
    private async Task ReceiveConvertAsync(Connection connection, byte[] buffer, WebSocketReceiveResult first, CancellationToken stoppingToken)
    {
        var headerBuffer = new MemoryStream();
        JsonObject? header = null;
        FileStream? file = null;
        string? directory = null;
        string? pdfPath = null;
        var result = first;
        try
        {
            while (true)
            {
                var chunk = buffer.AsMemory(0, result.Count);
                if (header is null)
                {
                    headerBuffer.Write(chunk.Span);
                    if (RemoteOcrProtocol.TryDecodeBinaryHeader(headerBuffer.GetBuffer().AsSpan(0, (int)headerBuffer.Length), out header, out var consumed))
                    {
                        var id = (string?)header!["id"] ?? Guid.NewGuid().ToString("N");
                        directory = ConvertRequestHandler.RequestDirectory(options.TempRoot, id);
                        Directory.CreateDirectory(directory);
                        pdfPath = Path.Combine(directory, ConvertRequestHandler.SafePdfName((string?)header["pdf_name"]));
                        file = new FileStream(pdfPath, FileMode.Create, FileAccess.Write, FileShare.None, bufferSize: 64 * 1024, useAsync: true);
                        var rest = headerBuffer.GetBuffer().AsMemory(consumed, (int)headerBuffer.Length - consumed);
                        await file.WriteAsync(rest, stoppingToken);
                    }
                }
                else
                {
                    await file!.WriteAsync(chunk, stoppingToken);
                }

                if (result.EndOfMessage)
                {
                    break;
                }

                result = await connection.Socket.ReceiveAsync(buffer, stoppingToken);
                if (result.MessageType != WebSocketMessageType.Binary)
                {
                    throw new InvalidDataException("mensagem binária interrompida por um frame de outro tipo");
                }
            }
        }
        catch (Exception ex) when (ex is FormatException or InvalidDataException or IOException)
        {
            logger.LogError("convert inválido do servidor: {Error}", ex.Message);
            if (file is not null)
            {
                await file.DisposeAsync();
            }

            if (directory is not null)
            {
                ConvertRequestHandler.Cleanup(directory);
            }

            if (header is not null && (string?)header["id"] is { } badId)
            {
                await connection.TrySendJsonAsync(ConvertRequestHandler.ErrorResult(badId, null, "INTERNAL", $"pedido inválido: {ex.Message}"));
            }

            return;
        }

        if (file is not null)
        {
            await file.DisposeAsync();
        }

        if (header is null)
        {
            logger.LogError("convert do servidor terminou antes do cabeçalho completo ({Bytes} bytes)", headerBuffer.Length);
            return;
        }

        var inFlight = new InFlight((string?)header["id"] ?? "", Path.GetFileName(pdfPath!), directory!);
        _inFlight[inFlight.Id] = inFlight;
        inFlight.Task = HandleConvertAsync(connection, header, pdfPath!, inFlight);
    }

    private async Task HandleConvertAsync(Connection connection, JsonObject header, string pdfPath, InFlight inFlight)
    {
        var stopwatch = Stopwatch.StartNew();
        OcrServerProcess? process = null;
        var failedToStart = false;
        try
        {
            if (!pool.TryAcquire(out process) || process is null)
            {
                // O Workers só manda até `instances` documentos simultâneos; chegar aqui é corrida entre uma queda de
                // processo e o aviso `instances`. O Workers devolve o item à fila.
                logger.LogWarning("sem processo livre para {Pdf} ({Id}); devolvendo BUSY", inFlight.PdfName, inFlight.Id);
                await connection.TrySendJsonAsync(ConvertRequestHandler.ErrorResult(inFlight.Id, null, "BUSY", "nenhum processo convert_ocr livre neste cliente"));
                return;
            }

            inFlight.Process = process;
            logger.LogInformation("{Label} ← {Pdf} ({Id})", process.Label, inFlight.PdfName, inFlight.Id);
            var (result, errorCode) = await ConvertRequestHandler.RunAsync(header, pdfPath, process, CancellationToken.None);
            failedToStart = errorCode == "START";
            stopwatch.Stop();

            var summary = ConvertRequestHandler.Summary(result, inFlight.PdfName, stopwatch.Elapsed);
            if (inFlight.Discard)
            {
                logger.LogInformation("{Label} {Summary} (descartado: cancelado pelo servidor)", process.Label, summary);
                return;
            }

            if (connection != _connection || connection.Socket.State != WebSocketState.Open)
            {
                logger.LogWarning("{Label} {Summary} (descartado: conexão caiu; o Workers reenviará)", process.Label, summary);
                return;
            }

            if (errorCode is null)
            {
                logger.LogInformation("{Label} {Summary}", process.Label, summary);
            }
            else
            {
                logger.LogWarning("{Label} {Summary}", process.Label, summary);
            }

            await connection.TrySendJsonAsync(result);
        }
        catch (Exception ex)
        {
            logger.LogError("{Label}: falha não tratada em {Pdf} ({Id}): {Error}", process?.Label ?? "?", inFlight.PdfName, inFlight.Id, ex.Message);
            logger.LogDebug(ex, "stack trace");
            await connection.TrySendJsonAsync(ConvertRequestHandler.ErrorResult(inFlight.Id, process, "INTERNAL", $"{ex.GetType().Name}: {ex.Message}"));
        }
        finally
        {
            if (process is not null)
            {
                pool.Release(process, failedToStart);
            }

            _inFlight.TryRemove(inFlight.Id, out _);
            if (!ConvertRequestHandler.Cleanup(inFlight.Directory))
            {
                logger.LogDebug("não foi possível apagar {Dir}", inFlight.Directory);
            }
        }
    }

    // ------------------------------------------------------------------ desligamento

    public override async Task StopAsync(CancellationToken cancellationToken)
    {
        _stopping = true;
        var connection = _connection;
        var inFlight = _inFlight.Values.Select(f => f.Task).Where(t => t is not null).ToArray();
        if (connection is not null)
        {
            await connection.TrySendJsonAsync(new JsonObject { ["type"] = RemoteOcrProtocol.Types.Bye, ["reason"] = "shutdown" });
        }

        if (inFlight.Length > 0)
        {
            logger.LogInformation("desligando: esperando {Count} documento(s) em voo por até {Seconds}s", inFlight.Length, options.ShutdownGraceSeconds);
            try
            {
                await Task.WhenAll(inFlight!).WaitAsync(TimeSpan.FromSeconds(options.ShutdownGraceSeconds), cancellationToken);
            }
            catch (TimeoutException)
            {
                logger.LogWarning("desligando: {Count} documento(s) não terminaram no prazo; o Workers os reenviará", _inFlight.Count);
            }
            catch (OperationCanceledException)
            {
            }
        }

        if (connection is not null)
        {
            await connection.CloseOutputQuietlyAsync(WebSocketCloseStatus.NormalClosure, "shutdown");
        }

        await base.StopAsync(cancellationToken);
        await pool.ShutdownAsync(TimeSpan.FromSeconds(10));
    }

    // ------------------------------------------------------------------ tipos internos

    private sealed class InFlight(string id, string pdfName, string directory)
    {
        public string Id { get; } = id;

        public string PdfName { get; } = pdfName;

        public string Directory { get; } = directory;

        public OcrServerProcess? Process { get; set; }

        public volatile bool Discard;

        public Task? Task { get; set; }
    }

    /// <summary>Um socket conectado e o gate que serializa os envios.</summary>
    private sealed class Connection(ClientWebSocket socket)
    {
        private readonly SemaphoreSlim _sendGate = new(1, 1);

        public ClientWebSocket Socket { get; } = socket;

        public async Task SendJsonAsync(JsonObject message, CancellationToken cancellationToken)
        {
            var bytes = RemoteOcrProtocol.EncodeText(message);
            await _sendGate.WaitAsync(cancellationToken);
            try
            {
                await Socket.SendAsync(bytes, WebSocketMessageType.Text, endOfMessage: true, cancellationToken);
            }
            finally
            {
                _sendGate.Release();
            }
        }

        public async Task TrySendJsonAsync(JsonObject message)
        {
            try
            {
                using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));
                await SendJsonAsync(message, cts.Token);
            }
            catch (Exception)
            {
                // conexão caiu: quem recebe o laço de recepção trata.
            }
        }

        /// <summary>Primeira mensagem de texto (hello_ok/reject) dentro do prazo; null se o servidor fechou.</summary>
        public async Task<JsonObject?> ReceiveTextAsync(TimeSpan timeout, CancellationToken cancellationToken)
        {
            var buffer = new byte[16 * 1024];
            var receive = ReceiveWholeAsync(buffer, cancellationToken);
            var completed = await Task.WhenAny(receive, Task.Delay(timeout, cancellationToken));
            if (completed != receive)
            {
                Socket.Abort();
                throw new TimeoutException($"servidor não respondeu ao hello em {timeout.TotalSeconds:0}s");
            }

            var (type, payload) = await receive;
            return type == WebSocketMessageType.Text ? RemoteOcrProtocol.ParseText(payload) : null;
        }

        private async Task<(WebSocketMessageType Type, byte[] Payload)> ReceiveWholeAsync(byte[] buffer, CancellationToken cancellationToken)
        {
            var result = await Socket.ReceiveAsync(buffer, cancellationToken);
            if (result.MessageType == WebSocketMessageType.Close)
            {
                return (WebSocketMessageType.Close, []);
            }

            return (result.MessageType, await ReceiveRestAsync(buffer, result, cancellationToken));
        }

        /// <summary>Junta os frames restantes de uma mensagem cujo primeiro frame já foi lido.</summary>
        public async Task<byte[]> ReceiveRestAsync(byte[] buffer, WebSocketReceiveResult first, CancellationToken cancellationToken)
        {
            using var message = new MemoryStream();
            message.Write(buffer, 0, first.Count);
            var result = first;
            while (!result.EndOfMessage)
            {
                result = await Socket.ReceiveAsync(buffer, cancellationToken);
                message.Write(buffer, 0, result.Count);
                if (message.Length > 64L * 1024 * 1024)
                {
                    throw new InvalidDataException("mensagem de texto acima de 64 MB");
                }
            }

            return message.ToArray();
        }

        public async Task CloseOutputQuietlyAsync(WebSocketCloseStatus status, string reason)
        {
            try
            {
                using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
                if (Socket.State is WebSocketState.Open or WebSocketState.CloseReceived)
                {
                    await Socket.CloseOutputAsync(status, reason, cts.Token);
                }
            }
            catch (Exception)
            {
                // já fechado.
            }
        }
    }
}
