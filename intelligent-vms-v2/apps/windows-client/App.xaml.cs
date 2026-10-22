using System.Globalization;
using System.Windows;
using System.Windows.Data;
using IntelligentVms.Client.ViewModels;
using IntelligentVms.Client.Views;

namespace IntelligentVms.Client;

public partial class App : Application
{
    private LoginViewModel? _login;
    private LiveViewModel? _live;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        _login = new LoginViewModel();
        if (new LoginView(_login).ShowDialog() != true || _login.Client is null)
        {
            Shutdown();
            return;
        }
        _live = new LiveViewModel(_login.Client);
        MainWindow = new MainWindow(_live);
        ShutdownMode = ShutdownMode.OnMainWindowClose;
        MainWindow.Show();
    }

    protected override void OnExit(ExitEventArgs e)
    {
        _live?.Dispose();
        _login?.Dispose();
        base.OnExit(e);
    }
}

public sealed class NullToCollapsedConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, CultureInfo culture) =>
        value is null ? Visibility.Collapsed : Visibility.Visible;

    public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture) =>
        throw new NotSupportedException();
}
