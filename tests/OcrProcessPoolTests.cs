using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging.Abstractions;
using RemoteOcr.Tools;

namespace RemoteOcr.Tests;

public sealed class OcrProcessPoolTests
{
    [Fact]
    public async Task Sobe_todos_os_processos_e_entrega_um_por_pedido()
    {
        var fakes = Enumerable.Range(0, 3).Select(_ => new FakeChildProcess(autoReady: true)).ToArray();
        var pool = new OcrProcessPool(TestOptions.Create(TestOptions.TempDirectory(), instances: 3), NullLogger<OcrProcessPool>.Instance, i => fakes[i]);

        await pool.StartAllAsync(CancellationToken.None);

        Assert.Equal(3, pool.Instances);
        Assert.Equal(3, pool.ReadyCount);
        Assert.Equal(3, pool.Free);
        Assert.All(fakes, f => Assert.True(f.Started));
        Assert.NotNull(pool.AnyReady);

        var acquired = new List<OcrServerProcess>();
        while (pool.TryAcquire(out var p))
        {
            acquired.Add(p!);
        }

        Assert.Equal([0, 1, 2], acquired.Select(p => p.Index).Order());
        Assert.False(pool.TryAcquire(out _));

        pool.Release(acquired[0]);
        Assert.True(pool.TryAcquire(out var again));
        Assert.Same(acquired[0], again);
    }

    [Fact]
    public async Task Processo_que_nao_sobe_fica_fora_e_volta_na_tentativa_periodica()
    {
        var attempts = 0;
        var fakes = new FakeChildProcess?[] { new(autoReady: true), null };
        var events = new List<(int Count, string Reason)>();
        var pool = new OcrProcessPool(TestOptions.Create(TestOptions.TempDirectory(), instances: 2), NullLogger<OcrProcessPool>.Instance,
            i => i == 0 ? fakes[0]! : new FakeChildProcess(failToStart: attempts++ == 0, autoReady: true),
            retryDownInterval: TimeSpan.FromMilliseconds(200));
        pool.ReadyCountChanged += (n, reason) => { lock (events) { events.Add((n, reason)); } };

        using var cts = new CancellationTokenSource();
        await pool.StartAllAsync(cts.Token);
        Assert.Equal(1, pool.ReadyCount);
        lock (events)
        {
            Assert.Equal([(1, "convert_ocr#1 subiu")], events);   // a subida inicial também avisa: o hello pode ter saído com 0
        }

        await WaitUntilAsync(() => pool.ReadyCount == 2);
        lock (events)
        {
            Assert.Equal((2, "convert_ocr#2 subiu"), events.Last());
        }

        Assert.Equal(2, pool.Free);
        cts.Cancel();
    }

    [Fact]
    public async Task Release_com_falha_de_reinicio_tira_o_processo_da_capacidade()
    {
        var fake = new FakeChildProcess(autoReady: true);
        var pool = new OcrProcessPool(TestOptions.Create(TestOptions.TempDirectory()), NullLogger<OcrProcessPool>.Instance, _ => fake, TimeSpan.FromHours(1));
        var events = new List<int>();
        pool.ReadyCountChanged += (n, _) => events.Add(n);
        await pool.StartAllAsync(CancellationToken.None);
        Assert.True(pool.TryAcquire(out var process));

        pool.Release(process!, failedToStart: true);

        Assert.Equal(0, pool.ReadyCount);
        Assert.Equal(0, pool.Free);
        Assert.Equal([1, 0], events);
    }

    [Fact]
    public async Task Health_traz_um_registro_por_processo_com_o_status_da_ferramenta()
    {
        var fakes = new[] { new FakeChildProcess(autoReady: true) { Done = 7 }, new FakeChildProcess(failToStart: true) };
        var pool = new OcrProcessPool(TestOptions.Create(TestOptions.TempDirectory(), instances: 2), NullLogger<OcrProcessPool>.Instance, i => fakes[i], TimeSpan.FromHours(1));
        await pool.StartAllAsync(CancellationToken.None);

        var health = await pool.HealthAsync(TimeSpan.FromSeconds(5));

        Assert.False((bool?)health["ok"]);
        Assert.Equal(2, (int?)health["instances_total"]);
        Assert.Equal(1, (int?)health["free"]);
        Assert.Equal(0, (int?)health["in_flight"]);
        var instances = (JsonArray)health["instances"]!;
        Assert.Equal(2, instances.Count);
        var first = (JsonObject)instances[0]!;
        Assert.True((bool?)first["ready"]);
        Assert.Equal(7, (int?)first["done"]);
        Assert.Equal(812.5, (double?)first["rss_mb"]);
        Assert.Equal(fakes[0].Id, (int?)first["pid"]);
        var second = (JsonObject)instances[1]!;
        Assert.False((bool?)second["ready"]);
        Assert.Null(second["pid"]?.GetValue<int?>());
    }

    [Fact]
    public void Linha_de_comando_e_ambiente_do_processo()
    {
        var storage = TestOptions.TempDirectory();
        var options = TestOptions.Create(storage, instances: 2) with { Python = "python", ExtraArgs = "--ocr-threads 2" };

        var info = OcrProcessPool.BuildStartInfo(options);

        Assert.Equal("python", info.FileName);
        Assert.Equal($"-m convert_ocr server --storage \"{Path.GetFullPath(storage)}\" --engine auto --ocr-device cpu --ocr-threads 2", info.Arguments);
        Assert.Equal(Path.GetFullPath(storage), info.WorkingDirectory);
        Assert.Equal("utf-8", info.Environment["PYTHONIOENCODING"]);
        Assert.Equal("1", info.Environment["PYTHONUNBUFFERED"]);
        Assert.Equal("1", info.Environment["NO_COLOR"]);
    }

    [Fact]
    public void Bibliotecas_nvidia_do_venv_entram_no_LD_LIBRARY_PATH()
    {
        var root = TestOptions.TempDirectory();
        var venv = Path.Combine(root, ".venv");
        File.WriteAllText(Path.Combine(Directory.CreateDirectory(venv).FullName, "pyvenv.cfg"), "home = /usr/bin");
        var python = Path.Combine(venv, "bin", "python");
        Directory.CreateDirectory(Path.GetDirectoryName(python)!);
        File.WriteAllText(python, "");
        foreach (var package in new[] { "cu13", "cudnn" })
        {
            var lib = Path.Combine(venv, "lib", "python3.13", "site-packages", "nvidia", package, "lib");
            Directory.CreateDirectory(lib);
            File.WriteAllText(Path.Combine(lib, $"lib{package}.so.1"), "");
        }
        Directory.CreateDirectory(Path.Combine(venv, "lib", "python3.13", "site-packages", "nvidia", "vazio", "lib"));

        var dirs = OcrProcessPool.NvidiaLibraryDirectories(python);

        Assert.Equal(2, dirs.Count);
        Assert.All(dirs, d => Assert.EndsWith("lib", d));
        Assert.Contains(dirs, d => d.Contains("cu13"));
        Assert.DoesNotContain(dirs, d => d.Contains("vazio"));

        if (!OperatingSystem.IsWindows())
        {
            var info = OcrProcessPool.BuildStartInfo(TestOptions.Create(root) with { Python = python });
            Assert.Equal(string.Join(Path.PathSeparator, dirs), info.Environment["LD_LIBRARY_PATH"]!.Split(Path.PathSeparator).Take(2).Aggregate((a, b) => a + Path.PathSeparator + b));
        }

        Assert.Empty(OcrProcessPool.NvidiaLibraryDirectories("python"));
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
}
