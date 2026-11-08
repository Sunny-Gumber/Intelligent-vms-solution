using System.Windows;
using CommunityToolkit.Mvvm.Input;
using IntelligentVms.Client.Services;

namespace IntelligentVms.Client.ViewModels;

public sealed class LoginViewModel : ViewModelBase, IDisposable
{
    private string _serverUrl = "https://localhost:8443";
    private string _username = "";
    private string _password = "";
    private string _totpCode = "";
    private string? _errorMessage;
    private bool _needsMfa;
    private string _statusMessage = "";

    public event EventHandler? SignInSucceeded;
    public ApiClient? Client { get; private set; }
    public string BuildBanner => "0.1.0-alpha — qualification pending";
    public string ServerUrl { get => _serverUrl; set => SetField(ref _serverUrl, value); }
    public string Username { get => _username; set => SetField(ref _username, value); }
    public string TotpCode { get => _totpCode; set => SetField(ref _totpCode, value); }
    public string? ErrorMessage => _errorMessage;
    public string StatusMessage => _statusMessage;
    public Visibility MfaVisibility => _needsMfa ? Visibility.Visible : Visibility.Collapsed;
    public Visibility ErrorVisibility => _errorMessage is null ? Visibility.Collapsed : Visibility.Visible;
    public IAsyncRelayCommand SignInCommand { get; }

    public LoginViewModel() => SignInCommand = new AsyncRelayCommand(SignInAsync);

    public void SetPassword(string password) => _password = password;

    private async Task SignInAsync()
    {
        _errorMessage = null;
        _statusMessage = "Signing in…";
        NotifyStatus();
        try
        {
            if (!Uri.TryCreate(ServerUrl, UriKind.Absolute, out var server) ||
                server.Scheme != Uri.UriSchemeHttps || !string.IsNullOrEmpty(server.UserInfo))
                throw new InvalidOperationException("Enter an HTTPS server URL without credentials.");

            Client?.Dispose();
            Client = new ApiClient(server);
            await Client.LoginAsync(Username, _password, _needsMfa ? TotpCode : null);
            _password = "";
            SignInSucceeded?.Invoke(this, EventArgs.Empty);
        }
        catch (VmsApiException ex) when (ex.Code == "mfa_required")
        {
            _needsMfa = true;
            _errorMessage = "Enter the code from your authenticator app.";
            OnPropertyChanged(nameof(MfaVisibility));
        }
        catch (Exception ex)
        {
            _errorMessage = ex.Message;
        }
        finally
        {
            _statusMessage = "";
            NotifyStatus();
        }
    }

    private void NotifyStatus()
    {
        OnPropertyChanged(nameof(ErrorMessage));
        OnPropertyChanged(nameof(ErrorVisibility));
        OnPropertyChanged(nameof(StatusMessage));
    }

    public void Dispose()
    {
        _password = "";
        Client?.Dispose();
    }
}
