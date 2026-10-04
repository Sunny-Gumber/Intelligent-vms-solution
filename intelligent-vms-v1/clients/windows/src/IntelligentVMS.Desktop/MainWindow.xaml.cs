using System.IO;
using System.Collections.ObjectModel;
using System.Windows;
using System.Windows.Controls;

namespace IntelligentVMS.Desktop;

public partial class MainWindow : Window
{
    private readonly IClientLogger _logger;
    private readonly JsonProfileStore _profiles=new();
    private readonly DesktopSession _session;
    private readonly VmsApiClient _api;
    private readonly ObservableCollection<ServerProfile> _profileItems=[];
    private ServerProfile? _activeProfile;
    private ServerCapabilities? _capabilities;
    private CameraInfo? _selectedCamera;
    private LiveSessionController? _live;
    private ServerConnectionState _connectionState=ServerConnectionState.Disconnected;

    public MainWindow(IClientLogger logger)
    {
        InitializeComponent();_logger=logger;_session=new DesktopSession(new WindowsCredentialStore(),logger);_api=new VmsApiClient(()=>_session.AccessToken);
        Loaded+=MainWindow_Loaded;Closing+=MainWindow_Closing;SchemeCombo.SelectedIndex=0;
    }

    private async void MainWindow_Loaded(object sender,RoutedEventArgs e)
    {
        foreach(var row in await _profiles.LoadAsync())_profileItems.Add(row);
        ProfileCombo.ItemsSource=_profileItems;
        if(_profileItems.Count>0)ProfileCombo.SelectedIndex=0;
        RefreshDiagnostics();
    }
    private async void ProfileCombo_SelectionChanged(object sender,SelectionChangedEventArgs e)
    {
        if(ProfileCombo.SelectedItem is not ServerProfile p)return;
        if(_activeProfile is not null && (_activeProfile.Id!=p.Id || _activeProfile.SafeAddress!=p.SafeAddress))
            await EndSessionForServerChangeAsync();
        _activeProfile=p;DisplayNameBox.Text=p.DisplayName;SelectScheme(p.Scheme);HostBox.Text=p.Host;PortBox.Text=p.Port.ToString(System.Globalization.CultureInfo.InvariantCulture);
        _api.Configure(p);_connectionState=ServerConnectionState.Disconnected;UpdateServerState();
        if(await _session.TryRestoreAsync(p.Id,ct=>_api.GetSessionAsync(ct)))await RefreshServerStateAsync();
    }
    private async void NewProfile_Click(object sender,RoutedEventArgs e)
    {
        if(_activeProfile is not null)await EndSessionForServerChangeAsync();
        ProfileCombo.SelectedItem=null;_activeProfile=null;DisplayNameBox.Text="";SelectScheme("https");HostBox.Text="";PortBox.Text="443";
        _connectionState=ServerConnectionState.Disconnected;UpdateServerState();SettingsMessage.Text="Enter a new VMS server profile.";
    }
    private async void SaveProfile_Click(object sender,RoutedEventArgs e)
    {
        if(!TryBuildProfile(out var p,out var error)){SettingsMessage.Text=error;return;}var old=_profileItems.FirstOrDefault(x=>x.Id==p.Id);if(old is not null)_profileItems.Remove(old);_profileItems.Add(p);await _profiles.SaveAsync(_profileItems);ProfileCombo.SelectedItem=p;SettingsMessage.Text="Client-local server profile saved.";
    }
    private async void TestConnection_Click(object sender,RoutedEventArgs e)
    {
        if(!TryBuildProfile(out var p,out var error)){SettingsMessage.Text=error;return;}
        SettingsMessage.Text="Connecting…";
        using var probe=new VmsApiClient(()=>null);probe.Configure(p);
        var result=await probe.TestConnectionAsync();SettingsMessage.Text=FriendlyConnection(result);
    }
    private async void SignIn_Click(object sender,RoutedEventArgs e)
    {
        if(_activeProfile is null){ServerStateText.Text="Configure and save a VMS server profile first.";return;}
        try{await StopLiveAsync();await _session.AuthenticateAsync(_activeProfile.Id,TokenBox.Password,RememberCheck.IsChecked==true,ct=>_api.GetSessionAsync(ct));TokenBox.Clear();LogoutButton.IsEnabled=true;await RefreshServerStateAsync();}
        catch(SessionExpiredException){TokenBox.Clear();ServerStateText.Text="Your session is invalid or expired. Sign in again.";}
        catch(Exception ex){TokenBox.Clear();_logger.LogError("auth","desktop sign-in failed",ex);ServerStateText.Text="Unable to authenticate with the VMS server.";}
    }
    private async void Logout_Click(object sender,RoutedEventArgs e){await StopLiveAsync();await _session.LogoutAsync();ClearAuthorizedState();_connectionState=ServerConnectionState.AuthenticationRequired;UpdateServerState();}
    private async Task RefreshServerStateAsync()
    {
        try{_connectionState=ServerConnectionState.Connected;_capabilities=await _api.GetCapabilitiesAsync();PopulateCameraTree(await _api.GetCamerasAsync());EventsTab.Visibility=_capabilities.EventHistory?Visibility.Visible:Visibility.Collapsed;LogoutButton.IsEnabled=true;UpdateServerState();}
        catch(SessionExpiredException){await StopLiveAsync();await _session.MarkExpiredAsync();ClearAuthorizedState();_connectionState=ServerConnectionState.AuthenticationRequired;ServerStateText.Text="Your session has expired. Sign in again.";}
        catch(IncompatibleServerException){_connectionState=ServerConnectionState.Incompatible;ServerStateText.Text="The VMS server is incompatible with this client foundation.";}
        catch(Exception ex){_logger.LogError("server","server refresh failed",ex);_connectionState=ServerConnectionState.Unreachable;ServerStateText.Text="Unable to reach VMS server.";}
    }
    private void PopulateCameraTree(IEnumerable<CameraInfo> cameras)
    {
        CameraTree.Items.Clear();foreach(var site in CameraTreeBuilder.Build(cameras)){var root=new TreeViewItem{Header=site.SiteId,IsExpanded=true};foreach(var camera in site.Cameras)root.Items.Add(new TreeViewItem{Header=$"{camera.Name} · {(camera.Enabled?camera.DesiredState:"disabled")}",Tag=camera});CameraTree.Items.Add(root);}
    }
    private void CameraTree_SelectedItemChanged(object sender,RoutedPropertyChangedEventArgs<object> e)
    {
        if(e.NewValue is not TreeViewItem{Tag:CameraInfo camera})return;_selectedCamera=camera;LiveSelectionText.Text=$"{camera.Name} · {camera.SiteId}";StreamRoleCombo.ItemsSource=camera.AvailableLiveRoles;StreamRoleCombo.SelectedItem=camera.AvailableLiveRoles.Contains("sub")?"sub":camera.AvailableLiveRoles.FirstOrDefault()??"main";
    }
    private async void StartLive_Click(object sender,RoutedEventArgs e)
    {
        if(_selectedCamera is null||_activeProfile is null||StreamRoleCombo.SelectedItem is not string role){LiveSelectionText.Text="Select an authorized camera and stream role.";return;}
        try{_live??=new LiveSessionController(_api,LiveMedia,_activeProfile,_logger);await _live.StartAsync(_selectedCamera.Id,role);LiveSelectionText.Text=$"{_selectedCamera.Name} · {role.ToUpperInvariant()}";RefreshDiagnostics();}
        catch(SessionExpiredException){await StopLiveAsync();await _session.MarkExpiredAsync();ClearAuthorizedState();_connectionState=ServerConnectionState.AuthenticationRequired;LiveSelectionText.Text="Your session has expired. Sign in again.";}
        catch(Exception ex){_logger.LogError("live","live view start failed",ex);LiveSelectionText.Text="Live view is unavailable for the selected camera.";}
    }
    private async void StopLive_Click(object sender,RoutedEventArgs e)=>await StopLiveAsync();
    private async Task StopLiveAsync(){if(_live is not null)await _live.StopAsync();RefreshDiagnostics();}
    private async void ExportDiagnostics_Click(object sender,RoutedEventArgs e)
    {
        var folder=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),"IntelligentVMS-Diagnostics");var path=await DiagnosticsService.ExportAsync(DiagnosticsService.Build(_activeProfile,_connectionState,_capabilities,_live?.State??"IDLE"),folder);DiagnosticsText.Text=$"Redacted diagnostics exported to:{Environment.NewLine}{path}";
    }
    private void MainWindow_Closing(object? sender,System.ComponentModel.CancelEventArgs e){try{_live?.StopAsync().GetAwaiter().GetResult();}catch{} _api.Dispose();}
    private async Task EndSessionForServerChangeAsync()
    {
        await StopLiveAsync();
        if(_session.State!=DesktopSessionState.SignedOut)await _session.LogoutAsync();
        ClearAuthorizedState();
        _connectionState=ServerConnectionState.Disconnected;
    }
    private void ClearAuthorizedState()
    {
        _live=null;_selectedCamera=null;CameraTree.Items.Clear();StreamRoleCombo.ItemsSource=null;_capabilities=null;
        EventsTab.Visibility=Visibility.Collapsed;LogoutButton.IsEnabled=false;
    }
    private bool TryBuildProfile(out ServerProfile p,out string error)
    {
        var scheme=(SchemeCombo.SelectedItem as ComboBoxItem)?.Content?.ToString()??"https";if(!int.TryParse(PortBox.Text,out var port))port=0;p=new ServerProfile(_activeProfile?.Id??Guid.NewGuid(),DisplayNameBox.Text.Trim(),scheme,HostBox.Text.Trim(),port);var result=ServerProfileValidator.Validate(p);error=result.Message;return result.IsValid;
    }
    private void SelectScheme(string scheme)=>SchemeCombo.SelectedIndex=scheme=="http"?1:0;
    private void UpdateServerState(){ServerStateText.Text=$"{_activeProfile?.DisplayName??"No server"} · {_connectionState}";RefreshDiagnostics();}
    private void RefreshDiagnostics(){var d=DiagnosticsService.Build(_activeProfile,_connectionState,_capabilities,_live?.State??"IDLE");DiagnosticsText.Text=$"Version: {d.ApplicationVersion}\nOS: {d.OsVersion}\nArchitecture: {d.Architecture}\nServer: {d.ServerAddress}\nConnection: {d.ConnectionState}\nServer profile: {d.DeploymentProfile}\nMedia: {d.MediaState}\nLogs: {d.LogLocation}";}
    private static string FriendlyConnection(ServerConnectionState s)=>s switch{ServerConnectionState.Connected=>"VMS server is reachable.",ServerConnectionState.AuthenticationRequired=>"VMS server is reachable; authentication is required.",ServerConnectionState.TlsError=>"TLS/certificate validation failed. Trust must be fixed; validation is not bypassed.",ServerConnectionState.Incompatible=>"Server capability contract is incompatible.",_=>"Unable to reach VMS server."};
}
