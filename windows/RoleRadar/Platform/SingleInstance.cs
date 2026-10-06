using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;

namespace RoleRadar.Platform;

/// <summary>One app per user: opening it again (the Start menu, its shortcut) shows the running one's
/// window instead, and `RoleRadar.exe --quit` (the installer, before an update) asks it to quit.</summary>
public sealed class SingleInstance(string name) : IDisposable
{
    // A pipe's name is seen by every user on the PC: so it says whose it is.
    private readonly string pipe = $"RoleRadar-{name}-{Environment.UserName}".Replace(' ', '-');
    private readonly CancellationTokenSource stopping = new();
    private Mutex? running;

    /// <summary>"show" or "quit", from a later copy, on the UI thread's dispatcher.</summary>
    public event Action<string>? Message;

    [DllImport("user32.dll")]
    private static extern bool AllowSetForegroundWindow(int processId);

    /// <summary>Send the running app a message. False if none is running.</summary>
    public bool TellRunning(string message)
    {
        try
        {
            using var client = new NamedPipeClientStream(".", pipe, PipeDirection.Out);
            client.Connect(1000);
            AllowSetForegroundWindow(-1); // the program just opened may bring a window forward: pass that on
            using var writer = new StreamWriter(client);
            writer.WriteLine(message);
            return true;
        }
        catch (Exception error) when (error is TimeoutException or IOException or UnauthorizedAccessException)
        {
            return false;
        }
    }

    /// <summary>Become the running app. False if another copy is it already (opened at the same moment, say).</summary>
    public bool Claim()
    {
        running = new Mutex(true, $@"Local\{pipe}", out var first);
        return first;
    }

    /// <summary>Take messages from later copies.</summary>
    public void Listen(System.Windows.Threading.Dispatcher dispatcher)
    {
        _ = Task.Run(async () =>
        {
            while (!stopping.IsCancellationRequested)
            {
                try
                {
                    await using var server = new NamedPipeServerStream(pipe, PipeDirection.In, 1, PipeTransmissionMode.Byte,
                        PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
                    await server.WaitForConnectionAsync(stopping.Token);
                    using var reader = new StreamReader(server);
                    var message = (await reader.ReadLineAsync(stopping.Token))?.Trim();
                    if (!string.IsNullOrEmpty(message))
                    {
                        dispatcher.Invoke(() => Message?.Invoke(message));
                    }
                }
                catch (OperationCanceledException)
                {
                    return;
                }
                catch (IOException error)
                {
                    Log.Error("Couldn't take a message from another copy", error);
                    await Task.Delay(1000);
                }
            }
        });
    }

    public void Dispose()
    {
        stopping.Cancel();
        running?.Dispose();
    }
}
