using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using RemoteOcr;
using RemoteOcr.Client;
using RemoteOcr.Tools;
using Serilog;
using Serilog.Events;

// Linhas de log com acentos/símbolos: sem UTF-8 o console do Windows mostra "??".
Console.OutputEncoding = System.Text.Encoding.UTF8;

RemoteOcrOptions options;
try
{
    options = RemoteOcrOptions.Parse(args, Environment.GetEnvironmentVariable);
}
catch (OptionsException ex)
{
    if (ex.Help)
    {
        Console.WriteLine(ex.Message);
        return 0;
    }

    Console.Error.WriteLine($"remote-ocr: {ex.Message}");
    Console.Error.WriteLine();
    Console.Error.WriteLine(RemoteOcrOptions.Usage);
    return 2;
}

// Sem os args: a linha de comando é nossa (--host, --api_key...), não configuração do host.
var builder = Host.CreateApplicationBuilder();
var logLevel = Environment.GetEnvironmentVariable(RemoteOcrOptions.EnvPrefix + "LOG_LEVEL") switch
{
    "Debug" or "debug" => LogEventLevel.Debug,
    "Warning" or "warning" => LogEventLevel.Warning,
    _ => LogEventLevel.Information,
};
builder.Logging.ClearProviders();
builder.Services.AddSerilog(configuration => configuration
    .MinimumLevel.Is(logLevel)
    .MinimumLevel.Override("Microsoft", LogEventLevel.Warning)
    .Enrich.FromLogContext()
    .WriteTo.Console(outputTemplate: "{Timestamp:yyyy-MM-dd HH:mm:ss.fff} {Level:u3} {Message:lj}{NewLine}{Exception}"));

builder.Services.AddSingleton(options);
builder.Services.AddSingleton<OcrProcessPool>(sp => new OcrProcessPool(options, sp.GetRequiredService<ILogger<OcrProcessPool>>()));
builder.Services.AddHostedService<RemoteOcrClient>();
// SIGTERM: o cliente avisa bye, espera os documentos em voo (ShutdownGraceSeconds) e encerra os processos Python.
builder.Services.Configure<HostOptions>(host => host.ShutdownTimeout = TimeSpan.FromSeconds(options.ShutdownGraceSeconds + 30));

using var host = builder.Build();
var logger = host.Services.GetRequiredService<ILogger<Program>>();
logger.LogInformation("remote-ocr: cliente '{Name}' → {Host}; {Instances} instância(s), device {Device}, engine {Engine}; python '{Python}' em '{ToolDir}', storage '{Storage}'",
    options.Name, options.Host, options.Instances, options.Device, options.Engine, options.Python, options.ToolDir, Path.GetFullPath(options.Storage));

await host.RunAsync();
return Environment.ExitCode;

/// <summary>Ponto de entrada (para o ILogger&lt;Program&gt;).</summary>
public partial class Program;
