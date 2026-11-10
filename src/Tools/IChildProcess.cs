using System.Diagnostics;
using System.Text;

namespace RemoteOcr.Tools;

/// <summary>
/// O mínimo de System.Diagnostics.Process que o OcrServerProcess usa, atrás de uma interface para os testes trocarem o
/// python por um processo falso (streams em memória). Cópia de Workers/Tools/IChildProcess.cs do repositório workers.
/// </summary>
public interface IChildProcess : IDisposable
{
    /// <summary>Inicia o processo com stdin/stdout/stderr redirecionados. Lança se o executável não existir.</summary>
    void Start();

    int Id { get; }

    bool HasExited { get; }

    /// <summary>Código de saída; só válido depois de HasExited.</summary>
    int ExitCode { get; }

    TextWriter StandardInput { get; }

    TextReader StandardOutput { get; }

    TextReader StandardError { get; }

    Task WaitForExitAsync(CancellationToken cancellationToken);

    void Kill();
}

/// <summary>Implementação real sobre System.Diagnostics.Process.</summary>
public sealed class SystemChildProcess(ProcessStartInfo startInfo) : IChildProcess
{
    private static readonly Encoding Utf8NoBom = new UTF8Encoding(encoderShouldEmitUTF8Identifier: false);

    private readonly Process _process = new()
    {
        StartInfo = Configure(startInfo),
        EnableRaisingEvents = true,
    };

    private static ProcessStartInfo Configure(ProcessStartInfo info)
    {
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.RedirectStandardInput = true;
        info.RedirectStandardOutput = true;
        info.RedirectStandardError = true;
        info.StandardInputEncoding = Utf8NoBom;
        info.StandardOutputEncoding = Encoding.UTF8;
        info.StandardErrorEncoding = Encoding.UTF8;
        return info;
    }

    public void Start()
    {
        _process.Start();
        _process.StandardInput.AutoFlush = true;
    }

    public int Id => _process.Id;

    public bool HasExited => _process.HasExited;

    public int ExitCode => _process.ExitCode;

    public TextWriter StandardInput => _process.StandardInput;

    public TextReader StandardOutput => _process.StandardOutput;

    public TextReader StandardError => _process.StandardError;

    public Task WaitForExitAsync(CancellationToken cancellationToken) => _process.WaitForExitAsync(cancellationToken);

    public void Kill()
    {
        try
        {
            if (!_process.HasExited)
            {
                _process.Kill(entireProcessTree: true);
            }
        }
        catch (InvalidOperationException)
        {
            // já terminou entre o teste e o Kill.
        }
    }

    public void Dispose() => _process.Dispose();
}
