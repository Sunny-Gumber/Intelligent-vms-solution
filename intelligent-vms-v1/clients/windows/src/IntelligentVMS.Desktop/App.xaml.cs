using System.Threading;
using System.Windows;
using System.Windows.Threading;

namespace IntelligentVMS.Desktop;

public partial class App : Application
{
    private Mutex? _singleInstance;
    private BoundedFileLogger? _logger;

    protected override void OnStartup(StartupEventArgs e)
    {
        ClientPaths.EnsureCreated();
        _logger = new BoundedFileLogger(ClientPaths.LogDirectory);
        DispatcherUnhandledException += OnDispatcherUnhandledException;
        _singleInstance = new Mutex(true, @"Local\IntelligentVMS.Desktop", out var isOwner);
        if (!isOwner)
        {
            MessageBox.Show("Intelligent VMS Desktop is already running.", "Intelligent VMS");
            Shutdown(0);
            return;
        }
        if (e.Args.Contains("--smoke-test", StringComparer.OrdinalIgnoreCase))
        {
            try { ClientSmokeVerifier.Verify(); _logger.Info("startup", "desktop smoke startup completed"); Shutdown(0); }
            catch (Exception ex) { _logger.Error("startup", "desktop smoke startup failed", ex); Shutdown(2); }
            return;
        }
        _logger.Info("startup", "desktop client starting");
        MainWindow = new MainWindow(_logger);
        ShutdownMode = ShutdownMode.OnMainWindowClose;
        MainWindow.Show();
        base.OnStartup(e);
    }

    protected override void OnExit(ExitEventArgs e)
    {
        _logger?.Info("shutdown", "desktop client stopped");
        try { _singleInstance?.ReleaseMutex(); } catch { }
        _singleInstance?.Dispose();
        base.OnExit(e);
    }

    private void OnDispatcherUnhandledException(object sender, DispatcherUnhandledExceptionEventArgs e)
    {
        _logger?.Error("ui", "unhandled desktop error", e.Exception);
        MessageBox.Show("Intelligent VMS encountered an unexpected error. Safe diagnostic details were written to the client log.",
            "Intelligent VMS", MessageBoxButton.OK, MessageBoxImage.Error);
        e.Handled = true;
        Shutdown(1);
    }
}
