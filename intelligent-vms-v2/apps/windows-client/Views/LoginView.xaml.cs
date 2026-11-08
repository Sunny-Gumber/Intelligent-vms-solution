using System.Windows;

using IntelligentVms.Client.ViewModels;

namespace IntelligentVms.Client.Views;

/// <summary>
/// Sign-in window.
///
/// The password is intentionally NOT data-bound. WPF's PasswordBox does not
/// expose Password as a DependencyProperty precisely so the plaintext is not
/// cached by the binding engine; we push it into the view model on change and
/// clear it as soon as authentication completes.
/// </summary>
public partial class LoginView : Window
{
    private readonly LoginViewModel _viewModel;

    public LoginView(LoginViewModel viewModel)
    {
        InitializeComponent();
        _viewModel = viewModel;
        DataContext = viewModel;

        _viewModel.SignInSucceeded += OnSignInSucceeded;
        Loaded += (_, _) => UsernameBox.Focus();
    }

    private void OnPasswordChanged(object sender, RoutedEventArgs e)
    {
        _viewModel.SetPassword(PasswordBox.Password);
    }

    private void OnSignInSucceeded(object? sender, System.EventArgs e)
    {
        PasswordBox.Clear();
        _viewModel.SetPassword(string.Empty);
        DialogResult = true;
        Close();
    }
}
