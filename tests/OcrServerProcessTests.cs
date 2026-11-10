using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using RemoteOcr.Tools;

namespace RemoteOcr.Tests;

public sealed class OcrServerProcessTests
{
    private static OcrProcessOptions Options(int requestTimeout = 5, int startupTimeout = 5) => new("python -m convert_ocr server", startupTimeout, requestTimeout);

    private static JsonObject Convert(string pdf = "/tmp/a.pdf") => new() { ["cmd"] = "convert", ["pdf"] = pdf, ["md"] = "/tmp/a.md", ["out"] = "/tmp/a.ocr.md" };

    [Fact]
    public async Task Start_espera_o_ready_e_guarda_os_detalhes()
    {
        var fake = new FakeChildProcess(autoReady: true);
        var process = new OcrServerProcess(() => fake, Options(), NullLogger.Instance, index: 2);

        await process.StartAsync(CancellationToken.None);

        Assert.True(fake.Started);
        Assert.Equal(1, process.Starts);
        Assert.True(process.IsRunning);
        Assert.Equal("convert_ocr#3", process.Label);
        Assert.Equal("0.3.0", (string?)process.Ready!["version"]);
        Assert.Contains("device.active=CUDAExecutionProvider", OcrServerProcess.ReadyDetails(process.Ready));
        Assert.Equal(fake.Id, process.Pid);
    }

    [Fact]
    public async Task Send_escreve_o_pedido_com_id_e_devolve_a_resposta_correlacionada()
    {
        var fake = new FakeChildProcess(autoReady: true)
        {
            OnRequest = request => Task.FromResult<JsonObject?>(new JsonObject { ["ok"] = true, ["status"] = "ok", ["pages"] = 3, ["pdf"] = request["pdf"]?.DeepClone() }),
        };
        var process = new OcrServerProcess(() => fake, Options(), NullLogger.Instance, 0);

        var response = await process.SendAsync(Convert("/tmp/x.pdf"), CancellationToken.None);

        Assert.Equal(3, (int?)response["pages"]);
        Assert.Equal("/tmp/x.pdf", (string?)response["pdf"]);
        var request = fake.Requests.Single(r => (string?)r["cmd"] == "convert");
        Assert.StartsWith($"{Environment.ProcessId}-0-", (string?)request["id"]);
        Assert.False(process.Busy);
    }

    [Fact]
    public async Task Status_e_respondido_mesmo_com_um_convert_em_voo()
    {
        var gate = new TaskCompletionSource<JsonObject?>(TaskCreationOptions.RunContinuationsAsynchronously);
        var fake = new FakeChildProcess(autoReady: true) { OnRequest = _ => gate.Task };
        var process = new OcrServerProcess(() => fake, Options(), NullLogger.Instance, 0);

        var convert = process.SendAsync(Convert(), CancellationToken.None);
        await Task.Delay(50);
        Assert.True(process.Busy);

        var status = await process.SendControlAsync("status", TimeSpan.FromSeconds(5));
        Assert.NotNull(status);
        Assert.Equal(812.5, (double?)status["rss_mb"]);

        gate.SetResult(new JsonObject { ["ok"] = true, ["status"] = "ok" });
        Assert.Equal("ok", (string?)(await convert)["status"]);
    }

    [Fact]
    public async Task Processo_que_morre_com_pedido_em_voo_falha_com_CRASH_e_o_proximo_pedido_reinicia()
    {
        var fakes = new Queue<FakeChildProcess>([new FakeChildProcess(), new FakeChildProcess(autoReady: true) { OnRequest = _ => Task.FromResult<JsonObject?>(new JsonObject { ["ok"] = true }) }]);
        var first = fakes.Peek();
        var process = new OcrServerProcess(() => fakes.Dequeue(), Options(), NullLogger.Instance, 0);

        var pending = process.SendAsync(Convert(), CancellationToken.None);
        first.Ready();
        await first.ReadRequestAsync();
        first.Exit(3);

        var crash = await Assert.ThrowsAsync<ToolException>(() => pending);
        Assert.Equal("CRASH", crash.Code);
        Assert.Contains("exit code 3", crash.Detail);

        var ok = await process.SendAsync(Convert(), CancellationToken.None);
        Assert.True((bool?)ok["ok"]);
        Assert.Equal(2, process.Starts);
    }

    [Fact]
    public async Task Reciclagem_reenvia_o_pedido_ao_processo_novo()
    {
        var first = new FakeChildProcess(autoReady: true) { OnRequest = _ => Task.FromResult<JsonObject?>(new JsonObject { ["ok"] = true, ["n"] = 1 }) };
        var second = new FakeChildProcess(autoReady: true) { OnRequest = _ => Task.FromResult<JsonObject?>(new JsonObject { ["ok"] = true, ["n"] = 2 }) };
        var fakes = new Queue<FakeChildProcess>([first, second]);
        var process = new OcrServerProcess(() => fakes.Dequeue(), Options(), NullLogger.Instance, 0);

        Assert.Equal(1, (int?)(await process.SendAsync(Convert(), CancellationToken.None))["n"]);

        // O processo avisa que vai sair (memória) e sai sem tratar mais nada: o pedido seguinte vai ao novo.
        first.OnRequest = _ => Task.FromResult<JsonObject?>(null);
        first.Emit("""{"event":"exit","reason":"memory","retry":true,"rss_mb":4200,"limit_mb":4096,"documents":57}""");
        await Task.Delay(50);
        first.Exit(4);

        var response = await process.SendAsync(Convert(), CancellationToken.None);
        Assert.Equal(2, (int?)response["n"]);
        Assert.Equal(2, process.Starts);
    }

    [Fact]
    public async Task Sem_resposta_no_prazo_mata_o_processo_e_falha_com_TIMEOUT()
    {
        var fake = new FakeChildProcess(autoReady: true) { OnRequest = _ => Task.FromResult<JsonObject?>(null) };
        var process = new OcrServerProcess(() => fake, Options(requestTimeout: 1), NullLogger.Instance, 0);

        var ex = await Assert.ThrowsAsync<ToolException>(() => process.SendAsync(Convert(), CancellationToken.None));

        Assert.Equal("TIMEOUT", ex.Code);
        Assert.True(fake.Killed);
        Assert.False(process.IsRunning);
    }

    [Fact]
    public async Task Falha_ao_iniciar_e_START_com_a_linha_de_comando()
    {
        var process = new OcrServerProcess(() => new FakeChildProcess(failToStart: true), Options(), NullLogger.Instance, 0);

        var ex = await Assert.ThrowsAsync<ToolException>(() => process.StartAsync(CancellationToken.None));

        Assert.Equal("START", ex.Code);
        Assert.Contains("python -m convert_ocr server", ex.Detail);
    }

    [Fact]
    public async Task Processo_que_sai_antes_do_ready_e_START()
    {
        var fake = new FakeChildProcess();
        var process = new OcrServerProcess(() => fake, Options(), NullLogger.Instance, 0);

        var start = process.StartAsync(CancellationToken.None);
        fake.EmitStderr("ModuleNotFoundError: No module named 'rapidocr'");
        fake.Exit(1);

        var ex = await Assert.ThrowsAsync<ToolException>(() => start);
        Assert.Equal("START", ex.Code);
        Assert.Contains("exit code 1", ex.Detail);
    }

    [Fact]
    public async Task Kill_derruba_o_processo_e_o_pedido_em_voo_falha_com_CRASH()
    {
        var fake = new FakeChildProcess(autoReady: true) { OnRequest = _ => Task.FromResult<JsonObject?>(null) };
        var process = new OcrServerProcess(() => fake, Options(), NullLogger.Instance, 0);

        var pending = process.SendAsync(Convert(), CancellationToken.None);
        await Task.Delay(50);
        process.Kill();

        var ex = await Assert.ThrowsAsync<ToolException>(() => pending);
        Assert.Equal("CRASH", ex.Code);
        Assert.True(fake.Killed);
    }

    [Fact]
    public async Task Shutdown_manda_o_comando_e_fecha_o_stdin()
    {
        var fake = new FakeChildProcess(autoReady: true);
        var process = new OcrServerProcess(() => fake, Options(), NullLogger.Instance, 0);
        await process.StartAsync(CancellationToken.None);

        await process.ShutdownAsync(TimeSpan.FromSeconds(5));

        Assert.True(fake.HasExited);
        Assert.False(fake.Killed);
        Assert.Contains(fake.Requests, r => (string?)r["cmd"] == "shutdown");
        Assert.False(process.IsRunning);
    }
}
