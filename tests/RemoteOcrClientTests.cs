using System.Net.WebSockets;
using System.Text;
using System.Text.Json.Nodes;
using System.Threading.Channels;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Hosting.Internal;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using RemoteOcr.Client;
using RemoteOcr.Protocol;
using RemoteOcr.Tools;

namespace RemoteOcr.Tests;

/// <summary>O cliente contra um Kestrel que faz o papel do Workers (aceita o WebSocket, confere a chave e deixa o teste falar o protocolo).</summary>
public sealed class RemoteOcrClientTests : IAsyncLifetime
{
    private FakeWorkersServer _server = null!;
    private readonly string _storage = TestOptions.TempDirectory();

    public async ValueTask InitializeAsync() => _server = await FakeWorkersServer.StartAsync("chave");

    public async ValueTask DisposeAsync()
    {
        await _server.DisposeAsync();
        if (Directory.Exists(_storage))
        {
            Directory.Delete(_storage, recursive: true);
        }
    }

    /// <summary>Cliente com processos falsos já prontos (o pool sobe antes de conectar, para o hello levar a contagem cheia).</summary>
    private async Task<(RemoteOcrClient Client, OcrProcessPool Pool, FakeChildProcess[] Fakes)> CreateClientAsync(int instances = 1, string apiKey = "chave", Func<int, FakeChildProcess>? factory = null)
    {
        var fakes = Enumerable.Range(0, instances).Select(i => factory?.Invoke(i) ?? new FakeChildProcess(autoReady: true)).ToArray();
        var options = TestOptions.Create(_storage, instances, _server.Uri.ToString()) with { ApiKey = apiKey, RequestTimeoutSeconds = 5 };
        var pool = new OcrProcessPool(options, NullLogger<OcrProcessPool>.Instance, i => fakes[i], TimeSpan.FromMilliseconds(200));
        await pool.StartAllAsync(CancellationToken.None);
        var client = new RemoteOcrClient(options, pool, new ApplicationLifetime(NullLogger<ApplicationLifetime>.Instance), NullLogger<RemoteOcrClient>.Instance);
        return (client, pool, fakes);
    }

    [Fact]
    public async Task Conecta_com_a_chave_manda_hello_com_as_instancias_e_o_device_e_aceita_hello_ok()
    {
        var (client, _, _) = await CreateClientAsync(instances: 2);
        await client.StartAsync(CancellationToken.None);

        var side = await _server.NextConnectionAsync();
        var hello = await side.ReceiveJsonAsync();

        Assert.Equal("hello", (string?)hello["type"]);
        Assert.Equal(1, (int?)hello["protocol"]);
        Assert.Equal("teste-01", (string?)hello["name"]);
        Assert.Equal(2, (int?)hello["instances"]);
        Assert.Equal("cuda", (string?)hello["device"]!["kind"]);
        Assert.Equal("0.3.0", (string?)hello["tool_version"]);
        Assert.Equal(Environment.MachineName, (string?)hello["hostname"]);

        await side.SendHelloOkAsync();
        await WaitUntilAsync(() => client.IsConnected);
        Assert.Equal(1, client.Connections);

        await client.StopAsync(CancellationToken.None);
        var bye = await side.ReceiveJsonAsync();
        Assert.Equal("bye", (string?)bye["type"]);
        side.Finish();
    }

    [Fact]
    public async Task Convert_binario_em_pedacos_vira_result_com_md_text_e_a_pasta_temporaria_e_apagada()
    {
        var pdfBytes = Enumerable.Range(0, 150_000).Select(i => (byte)(i % 253)).ToArray();
        string? seenPdfPath = null;
        var fake = new FakeChildProcess(autoReady: true);
        fake.OnRequest = async request =>
        {
            seenPdfPath = (string?)request["pdf"];
            Assert.Equal(pdfBytes, await File.ReadAllBytesAsync(seenPdfPath!));
            Assert.Equal("> __Página 1 de 1__\n\n[OCR:1]\n", await File.ReadAllTextAsync((string)request["md"]!));
            await File.WriteAllTextAsync((string)request["out"]!, "# OCR\n\nreconhecido\n");
            return new JsonObject { ["ok"] = true, ["status"] = "ok", ["pages"] = 1, ["ocr_done"] = 1, ["token_count"] = 4, ["ms"] = new JsonObject { ["total"] = 50 } };
        };
        var (client, _, _) = await CreateClientAsync(factory: _ => fake);
        await client.StartAsync(CancellationToken.None);
        var side = await _server.NextConnectionAsync();
        await side.ReceiveJsonAsync();
        await side.SendHelloOkAsync();

        await side.SendConvertAsync("42-1", "03-scan.pdf", "> __Página 1 de 1__\n\n[OCR:1]\n", pdfBytes, chunk: 10_000);
        var result = await side.ReceiveJsonAsync();

        Assert.Equal("result", (string?)result["type"]);
        Assert.Equal("42-1", (string?)result["id"]);
        Assert.True((bool?)result["ok"]);
        Assert.Equal("# OCR\n\nreconhecido\n", (string?)result["md_text"]);
        Assert.Equal(4, (int?)result["token_count"]);
        Assert.Equal("convert_ocr#1", (string?)result["instance"]);
        Assert.Equal(Path.Combine(_storage, "tmp", "42-1", "03-scan.pdf"), seenPdfPath);
        await WaitUntilAsync(() => !Directory.Exists(Path.GetDirectoryName(seenPdfPath!)));
        Assert.Equal(0, client.InFlightCount);

        await client.StopAsync(CancellationToken.None);
        side.Finish();
    }

    [Fact]
    public async Task Dois_converts_simultaneos_vao_para_processos_distintos_e_o_terceiro_e_BUSY()
    {
        var gates = new[] { new TaskCompletionSource<JsonObject?>(TaskCreationOptions.RunContinuationsAsynchronously), new TaskCompletionSource<JsonObject?>(TaskCreationOptions.RunContinuationsAsynchronously) };
        var (client, _, fakes) = await CreateClientAsync(instances: 2, factory: i => new FakeChildProcess(autoReady: true) { OnRequest = _ => gates[i].Task });
        await client.StartAsync(CancellationToken.None);
        var side = await _server.NextConnectionAsync();
        await side.ReceiveJsonAsync();
        await side.SendHelloOkAsync();

        await side.SendConvertAsync("1", "a.pdf", "[OCR:1]", "%PDF"u8.ToArray());
        await side.SendConvertAsync("2", "b.pdf", "[OCR:1]", "%PDF"u8.ToArray());
        await WaitUntilAsync(() => fakes.All(f => f.Requests.Any(r => (string?)r["cmd"] == "convert")));
        Assert.Equal(2, client.InFlightCount);

        await side.SendConvertAsync("3", "c.pdf", "[OCR:1]", "%PDF"u8.ToArray());
        var busy = await side.ReceiveJsonAsync();
        Assert.Equal("3", (string?)busy["id"]);
        Assert.False((bool?)busy["ok"]);
        Assert.Equal("BUSY", (string?)busy["error"]!["code"]);

        gates[0].SetResult(new JsonObject { ["ok"] = true, ["status"] = "no_ocr" });
        gates[1].SetResult(new JsonObject { ["ok"] = true, ["status"] = "no_ocr" });
        var r1 = await side.ReceiveJsonAsync();
        var r2 = await side.ReceiveJsonAsync();
        Assert.Equal(["1", "2"], new[] { (string?)r1["id"], (string?)r2["id"] }.Order());

        await client.StopAsync(CancellationToken.None);
        side.Finish();
    }

    [Fact]
    public async Task Cancel_com_kill_mata_o_processo_e_sem_kill_descarta_o_resultado()
    {
        var gate = new TaskCompletionSource<JsonObject?>(TaskCreationOptions.RunContinuationsAsynchronously);
        var (client, _, fakes) = await CreateClientAsync(instances: 2, factory: _ => new FakeChildProcess(autoReady: true) { OnRequest = _ => gate.Task });
        await client.StartAsync(CancellationToken.None);
        var side = await _server.NextConnectionAsync();
        await side.ReceiveJsonAsync();
        await side.SendHelloOkAsync();

        await side.SendConvertAsync("k1", "a.pdf", "[OCR:1]", "%PDF"u8.ToArray());
        await side.SendConvertAsync("d1", "b.pdf", "[OCR:1]", "%PDF"u8.ToArray());
        await WaitUntilAsync(() => fakes.Count(f => f.Requests.Any(r => (string?)r["cmd"] == "convert")) == 2);

        await side.SendJsonAsync(new JsonObject { ["type"] = "cancel", ["id"] = "k1", ["kill"] = true });
        await WaitUntilAsync(() => fakes.Any(f => f.Killed));
        await side.SendJsonAsync(new JsonObject { ["type"] = "cancel", ["id"] = "d1", ["kill"] = false });
        await Task.Delay(100);
        gate.SetResult(new JsonObject { ["ok"] = true, ["status"] = "no_ocr" });

        // Nenhum result chega: o morto e o descartado ficam em silêncio; um ping confirma que a conexão segue viva.
        await WaitUntilAsync(() => client.InFlightCount == 0);
        await side.SendJsonAsync(new JsonObject { ["type"] = "ping", ["ts"] = 9 });
        var pong = await side.ReceiveJsonAsync();
        Assert.Equal("pong", (string?)pong["type"]);
        Assert.Equal(9, (int?)pong["ts"]);

        await client.StopAsync(CancellationToken.None);
        side.Finish();
    }

    [Fact]
    public async Task Health_devolve_o_estado_de_cada_processo()
    {
        var (client, _, _) = await CreateClientAsync(instances: 2, factory: i => new FakeChildProcess(autoReady: true) { Done = i + 1 });
        await client.StartAsync(CancellationToken.None);
        var side = await _server.NextConnectionAsync();
        await side.ReceiveJsonAsync();
        await side.SendHelloOkAsync();

        await side.SendJsonAsync(new JsonObject { ["type"] = "health", ["id"] = "h1" });
        var health = await side.ReceiveJsonAsync();

        Assert.Equal("health_result", (string?)health["type"]);
        Assert.Equal("h1", (string?)health["id"]);
        Assert.True((bool?)health["ok"]);
        Assert.Equal(2, (int?)health["free"]);
        var instances = (JsonArray)health["instances"]!;
        Assert.Equal([1, 2], instances.Select(i => (int?)i!["done"]).Order());
        Assert.All(instances, i => Assert.True((bool?)i!["ready"]));

        await client.StopAsync(CancellationToken.None);
        side.Finish();
    }

    [Fact]
    public async Task Processo_que_sobe_depois_do_hello_gera_mensagem_instances()
    {
        // O processo 2 falha na primeira subida e sobe na tentativa periódica (200 ms): a fábrica do pool cria um
        // processo novo a cada tentativa, como o Python de verdade.
        var attempts = 0;
        var options = TestOptions.Create(_storage, instances: 2, _server.Uri.ToString());
        var pool = new OcrProcessPool(options, NullLogger<OcrProcessPool>.Instance,
            i => i == 0 ? new FakeChildProcess(autoReady: true) : new FakeChildProcess(failToStart: attempts++ == 0, autoReady: true),
            TimeSpan.FromMilliseconds(200));
        await pool.StartAllAsync(CancellationToken.None);
        Assert.Equal(1, pool.ReadyCount);
        var client = new RemoteOcrClient(options, pool, new ApplicationLifetime(NullLogger<ApplicationLifetime>.Instance), NullLogger<RemoteOcrClient>.Instance);
        await client.StartAsync(CancellationToken.None);
        var side = await _server.NextConnectionAsync();
        var hello = await side.ReceiveJsonAsync();
        Assert.Equal(1, (int?)hello["instances"]);
        await side.SendHelloOkAsync();

        JsonObject instances;
        do
        {
            instances = await side.ReceiveJsonAsync();
            Assert.Equal("instances", (string?)instances["type"]);
        }
        while ((int?)instances["instances"] != 2);

        // "subiu" se chegou depois do hello_ok; "sincronização" se subiu entre o hello e o hello_ok
        Assert.Contains((string?)instances["reason"], new[] { "convert_ocr#2 subiu", "sincronização" });
        Assert.Equal("cuda", (string?)instances["device"]!["kind"]);
        Assert.Equal("0.3.0", (string?)instances["tool_version"]);
        Assert.Equal(2, pool.ReadyCount);

        await client.StopAsync(CancellationToken.None);
        side.Finish();
    }

    [Fact]
    public async Task Servidor_que_cai_e_reconectado_com_backoff()
    {
        var (client, _, _) = await CreateClientAsync();
        await client.StartAsync(CancellationToken.None);
        var first = await _server.NextConnectionAsync();
        await first.ReceiveJsonAsync();
        await first.SendHelloOkAsync();
        await WaitUntilAsync(() => client.IsConnected);

        await first.CloseAsync(WebSocketCloseStatus.EndpointUnavailable, "workers desligando");
        first.Finish();

        var second = await _server.NextConnectionAsync(TimeSpan.FromSeconds(10));
        var hello = await second.ReceiveJsonAsync();
        Assert.Equal("hello", (string?)hello["type"]);
        await second.SendHelloOkAsync();
        await WaitUntilAsync(() => client.Connections == 2);

        await client.StopAsync(CancellationToken.None);
        second.Finish();
    }

    [Fact]
    public async Task Chave_errada_nao_conecta_e_o_cliente_continua_tentando()
    {
        var (client, _, _) = await CreateClientAsync(apiKey: "errada");
        await client.StartAsync(CancellationToken.None);

        await Task.Delay(500);
        Assert.False(client.IsConnected);
        Assert.Equal(0, client.Connections);
        Assert.True(_server.Rejected > 0);

        await client.StopAsync(CancellationToken.None);
    }

    [Fact]
    public async Task Reject_do_servidor_derruba_a_conexao_e_tenta_de_novo()
    {
        var (client, _, _) = await CreateClientAsync();
        await client.StartAsync(CancellationToken.None);
        var first = await _server.NextConnectionAsync();
        await first.ReceiveJsonAsync();
        await first.SendJsonAsync(new JsonObject { ["type"] = "reject", ["code"] = "PROTOCOL", ["message"] = "protocolo 1 esperado" });
        await first.CloseAsync(WebSocketCloseStatus.PolicyViolation, "protocolo");
        first.Finish();

        var second = await _server.NextConnectionAsync(TimeSpan.FromSeconds(10));
        Assert.Equal("hello", (string?)(await second.ReceiveJsonAsync())["type"]);
        Assert.Equal(0, client.Connections);

        await client.StopAsync(CancellationToken.None);
        second.Finish();
    }

    [Fact]
    public async Task Stop_espera_o_documento_em_voo_e_manda_o_resultado_antes_de_fechar()
    {
        var gate = new TaskCompletionSource<JsonObject?>(TaskCreationOptions.RunContinuationsAsynchronously);
        var (client, _, fakes) = await CreateClientAsync(factory: _ => new FakeChildProcess(autoReady: true) { OnRequest = _ => gate.Task });
        await client.StartAsync(CancellationToken.None);
        var side = await _server.NextConnectionAsync();
        await side.ReceiveJsonAsync();
        await side.SendHelloOkAsync();
        await side.SendConvertAsync("s1", "a.pdf", "[OCR:1]", "%PDF"u8.ToArray());
        await WaitUntilAsync(() => client.InFlightCount == 1);

        var stop = client.StopAsync(CancellationToken.None);
        var bye = await side.ReceiveJsonAsync();
        Assert.Equal("bye", (string?)bye["type"]);
        Assert.False(stop.IsCompleted);

        gate.SetResult(new JsonObject { ["ok"] = true, ["status"] = "no_ocr", ["pages"] = 2 });
        var result = await side.ReceiveJsonAsync();
        Assert.Equal("s1", (string?)result["id"]);
        await stop;
        Assert.True(fakes[0].HasExited);   // shutdown dos processos no fim
        side.Finish();
    }

    private static async Task WaitUntilAsync(Func<bool> condition, int timeoutMs = 5000)
    {
        var deadline = DateTime.UtcNow.AddMilliseconds(timeoutMs);
        while (!condition())
        {
            Assert.True(DateTime.UtcNow < deadline, "condição não satisfeita a tempo");
            await Task.Delay(10);
        }
    }

    /// <summary>Kestrel em porta efêmera que aceita /ws/remote-ocr com a chave e entrega cada socket ao teste.</summary>
    private sealed class FakeWorkersServer : IAsyncDisposable
    {
        private readonly Channel<ServerSide> _accepted = Channel.CreateUnbounded<ServerSide>();
        private WebApplication _app = null!;

        public Uri Uri { get; private set; } = null!;

        public int Rejected { get; private set; }

        public static async Task<FakeWorkersServer> StartAsync(string key)
        {
            var server = new FakeWorkersServer();
            var builder = WebApplication.CreateBuilder();
            builder.Logging.ClearProviders();
            builder.WebHost.UseUrls("http://127.0.0.1:0");
            server._app = builder.Build();
            server._app.UseWebSockets();
            server._app.Map(RemoteOcrProtocol.Path, async context =>
            {
                if (context.Request.Headers[RemoteOcrProtocol.ApiKeyHeader] != key)
                {
                    server.Rejected++;
                    context.Response.StatusCode = StatusCodes.Status401Unauthorized;
                    return;
                }

                if (!context.WebSockets.IsWebSocketRequest)
                {
                    context.Response.StatusCode = StatusCodes.Status400BadRequest;
                    return;
                }

                using var socket = await context.WebSockets.AcceptWebSocketAsync();
                var side = new ServerSide(socket);
                server._accepted.Writer.TryWrite(side);
                await side.Completed.Task;
            });
            await server._app.StartAsync();
            var address = server._app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.First();
            var http = new Uri(address);
            server.Uri = new Uri($"ws://{http.Host}:{http.Port}{RemoteOcrProtocol.Path}");
            return server;
        }

        public async Task<ServerSide> NextConnectionAsync(TimeSpan? timeout = null)
        {
            using var cts = new CancellationTokenSource(timeout ?? TimeSpan.FromSeconds(5));
            return await _accepted.Reader.ReadAsync(cts.Token);
        }

        public async ValueTask DisposeAsync()
        {
            await _app.StopAsync();
            await _app.DisposeAsync();
        }
    }

    private sealed class ServerSide(WebSocket socket)
    {
        public TaskCompletionSource Completed { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public void Finish() => Completed.TrySetResult();

        public Task SendJsonAsync(JsonObject message) =>
            socket.SendAsync(RemoteOcrProtocol.EncodeText(message), WebSocketMessageType.Text, endOfMessage: true, CancellationToken.None);

        public Task SendHelloOkAsync() => SendJsonAsync(new JsonObject
        {
            ["type"] = "hello_ok", ["protocol"] = 1, ["server"] = "workers-fake", ["total_instances"] = 1, ["request_timeout_s"] = 2100, ["max_pdf_mb"] = 200, ["max_result_mb"] = 64,
        });

        public async Task SendConvertAsync(string id, string pdfName, string md, byte[] pdf, int chunk = 64 * 1024)
        {
            var header = RemoteOcrProtocol.EncodeBinaryHeader(new JsonObject { ["type"] = "convert", ["id"] = id, ["pdf_name"] = pdfName, ["md"] = md, ["timeout_s"] = 1800 });
            await socket.SendAsync(header, WebSocketMessageType.Binary, endOfMessage: pdf.Length == 0, CancellationToken.None);
            for (var offset = 0; offset < pdf.Length; offset += chunk)
            {
                var size = Math.Min(chunk, pdf.Length - offset);
                await socket.SendAsync(pdf.AsMemory(offset, size), WebSocketMessageType.Binary, endOfMessage: offset + size >= pdf.Length, CancellationToken.None);
            }
        }

        public async Task<JsonObject> ReceiveJsonAsync(TimeSpan? timeout = null)
        {
            using var cts = new CancellationTokenSource(timeout ?? TimeSpan.FromSeconds(10));
            var buffer = new byte[64 * 1024];
            using var message = new MemoryStream();
            while (true)
            {
                var result = await socket.ReceiveAsync(buffer, cts.Token);
                Assert.NotEqual(WebSocketMessageType.Close, result.MessageType);
                message.Write(buffer, 0, result.Count);
                if (result.EndOfMessage)
                {
                    return (JsonObject)JsonNode.Parse(Encoding.UTF8.GetString(message.ToArray()))!;
                }
            }
        }

        public async Task CloseAsync(WebSocketCloseStatus status, string reason)
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            try
            {
                await socket.CloseAsync(status, reason, cts.Token);
            }
            catch (Exception ex) when (ex is WebSocketException or OperationCanceledException)
            {
            }
        }
    }
}
