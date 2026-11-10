using System.Text;
using System.Text.Json.Nodes;
using System.Threading.Channels;
using RemoteOcr.Tools;

namespace RemoteOcr.Tests;

/// <summary>
/// Processo falso: o teste faz o papel do convert_ocr. Lê o que o .NET escreveu no stdin via <see cref="ReadRequestAsync"/>
/// e responde com <see cref="Emit"/> (stdout) / <see cref="Exit"/>; ou, com <see cref="AutoReady"/> e
/// <see cref="OnRequest"/>, responde sozinho (ready na subida, ping/status prontos, convert pelo callback).
/// Cópia adaptada de tests/Workers/Tools/FakeChildProcess.cs do repositório workers.
/// </summary>
internal sealed class FakeChildProcess : IChildProcess
{
    private readonly Channel<string> _stdin = Channel.CreateUnbounded<string>();
    private readonly Channel<string> _stdout = Channel.CreateUnbounded<string>();
    private readonly Channel<string> _stderr = Channel.CreateUnbounded<string>();
    private readonly TaskCompletionSource _exited = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private static int _nextId = 1000;

    public FakeChildProcess(bool failToStart = false, bool autoReady = false)
    {
        FailToStart = failToStart;
        AutoReady = autoReady;
        StandardInput = new LineWriter(_stdin.Writer);
        StandardOutput = new ChannelReader(_stdout.Reader);
        StandardError = new ChannelReader(_stderr.Reader);
    }

    public bool FailToStart { get; }

    /// <summary>Emite o `ready` sozinho ao iniciar e responde ping/status; `convert` vai para <see cref="OnRequest"/>.</summary>
    public bool AutoReady { get; }

    /// <summary>Resposta automática a um `convert` (recebe o pedido, devolve a resposta sem o id — ele é copiado). Null = sem resposta.</summary>
    public Func<JsonObject, Task<JsonObject?>>? OnRequest { get; set; }

    public List<JsonObject> Requests { get; } = [];

    public bool Started { get; private set; }

    public bool Killed { get; private set; }

    public int Id { get; } = Interlocked.Increment(ref _nextId);

    public bool HasExited { get; private set; }

    public int ExitCode { get; private set; }

    public TextWriter StandardInput { get; }

    public TextReader StandardOutput { get; }

    public TextReader StandardError { get; }

    public int Done { get; set; }

    public void Start()
    {
        if (FailToStart)
        {
            throw new System.ComponentModel.Win32Exception("O sistema não pode encontrar o arquivo especificado");
        }

        Started = true;
        if (AutoReady)
        {
            Ready();
            _ = AutoRespondAsync();
        }
    }

    public Task WaitForExitAsync(CancellationToken cancellationToken) => _exited.Task.WaitAsync(cancellationToken);

    public void Kill() => Exit(-1, killed: true);

    public void Dispose()
    {
    }

    // -- lado "python" -----------------------------------------------------------------------

    public void Emit(string line) => _stdout.Writer.TryWrite(line);

    public void Emit(JsonObject message) => Emit(message.ToJsonString());

    public void EmitStderr(string line) => _stderr.Writer.TryWrite(line);

    public void Ready() => Emit($$"""{"event":"ready","version":"0.3.0","pid":{{Id}},"engine":"auto","backend":"rapidocr","device":{"kind":"cuda","active":"CUDAExecutionProvider","provider":"CUDAExecutionProvider"},"output":"replace","dpi":200,"timeout_s":1800.0,"max_rss_mb":4096,"pymupdf":"2.1","storage":"/storage","tiktoken":"o200k_base"}""");

    public void Exit(int code, bool killed = false)
    {
        if (HasExited)
        {
            return;
        }

        HasExited = true;
        ExitCode = code;
        Killed |= killed;
        _stdout.Writer.TryComplete();
        _stderr.Writer.TryComplete();
        _exited.TrySetResult();
    }

    public async Task<string> ReadRequestAsync(TimeSpan? timeout = null)
    {
        using var cts = new CancellationTokenSource(timeout ?? TimeSpan.FromSeconds(10));
        return await _stdin.Reader.ReadAsync(cts.Token);
    }

    public async Task<JsonObject> ReadRequestJsonAsync(TimeSpan? timeout = null) => (JsonObject)JsonNode.Parse(await ReadRequestAsync(timeout))!;

    /// <summary>Responde a um pedido com os campos informados mais o id dele.</summary>
    public void Reply(JsonObject request, JsonObject response)
    {
        response["id"] = request["id"]?.DeepClone();
        Emit(response);
    }

    private async Task AutoRespondAsync()
    {
        try
        {
            await foreach (var line in _stdin.Reader.ReadAllAsync())
            {
                if (JsonNode.Parse(line) is not JsonObject request)
                {
                    continue;
                }

                lock (Requests)
                {
                    Requests.Add(request);
                }

                switch ((string?)request["cmd"])
                {
                    case "ping":
                        Reply(request, new JsonObject { ["ok"] = true, ["pong"] = true, ["ts"] = 1 });
                        break;
                    case "status":
                        Reply(request, new JsonObject { ["ok"] = true, ["done"] = Done, ["errors"] = 0, ["timeouts"] = 0, ["ocr_pages"] = Done * 3, ["rss_mb"] = 812.5, ["max_rss_mb"] = 4096, ["uptime_s"] = 120, ["queued"] = 0, ["inflight"] = 0 });
                        break;
                    case "shutdown":
                        Exit(0);
                        return;
                    default:
                        // Como a ferramenta: o convert roda à parte e a leitura do stdin continua (ping/status respondem na hora).
                        _ = RespondConvertAsync(request);
                        break;
                }
            }
        }
        catch (ChannelClosedException)
        {
        }
    }

    private async Task RespondConvertAsync(JsonObject request)
    {
        try
        {
            if (OnRequest is { } handler && await handler(request) is { } response)
            {
                Done++;
                Reply(request, response);
            }
        }
        catch (Exception ex)
        {
            Reply(request, new JsonObject { ["ok"] = false, ["status"] = "error", ["error"] = new JsonObject { ["code"] = "INTERNAL", ["message"] = ex.Message } });
        }
    }

    /// <summary>Linhas completas escritas pelo .NET no stdin.</summary>
    private sealed class LineWriter(ChannelWriter<string> lines) : TextWriter
    {
        private readonly StringBuilder _buffer = new();

        public override Encoding Encoding => Encoding.UTF8;

        public override void Write(char value)
        {
            if (value == '\n')
            {
                lines.TryWrite(_buffer.ToString());
                _buffer.Clear();
            }
            else
            {
                _buffer.Append(value);
            }
        }

        public override void Write(string? value)
        {
            foreach (var c in value ?? "")
            {
                Write(c);
            }
        }

        public override Task WriteAsync(ReadOnlyMemory<char> buffer, CancellationToken cancellationToken = default)
        {
            foreach (var c in buffer.Span)
            {
                Write(c);
            }
            return Task.CompletedTask;
        }

        public override void Close() => lines.TryComplete();
    }

    private sealed class ChannelReader(ChannelReader<string> lines) : TextReader
    {
        public override async ValueTask<string?> ReadLineAsync(CancellationToken cancellationToken)
        {
            try
            {
                return await lines.ReadAsync(cancellationToken);
            }
            catch (ChannelClosedException)
            {
                return null;
            }
        }

        public override Task<string?> ReadLineAsync() => ReadLineAsync(CancellationToken.None).AsTask();
    }
}
