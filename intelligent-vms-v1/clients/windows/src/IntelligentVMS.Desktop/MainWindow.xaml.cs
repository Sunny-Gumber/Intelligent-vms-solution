using System.IO;
using System.Collections.ObjectModel;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Shapes;
using Microsoft.Win32;

namespace IntelligentVMS.Desktop;

[System.Diagnostics.CodeAnalysis.SuppressMessage("Design","CA1001",Justification="WPF window lifetime disposes owned client/coordinator/renderers in the Closing handler.")]
public partial class MainWindow:Window
{
    private readonly IClientLogger _logger;
    private readonly JsonProfileStore _profiles=new();
    private readonly LiveGridSettingsStore _gridSettings=new();
    private readonly DesktopSession _session;
    private readonly OidcAuthenticationManager _oidc;
    private readonly VmsApiClient _api;
    private readonly ObservableCollection<ServerProfile> _profileItems=[];
    private readonly List<LiveTileView> _tileViews=[];
    private Dictionary<string,CameraInfo> _authorizedCameras=new(StringComparer.Ordinal);
    private ServerProfile? _activeProfile;
    private AuthenticationCapabilities? _authCapabilities;
    private ServerCapabilities? _capabilities;
    private CameraInfo? _selectedCamera;
    private LiveGridCoordinator? _grid;
    private PlaybackCoordinator? _playback;
    private PtzCoordinator? _ptz;
    private bool _ptzPointerHeld;
    private ServerConnectionState _connectionState=ServerConnectionState.Disconnected;

    public MainWindow(IClientLogger logger)
    {
        InitializeComponent();
        _logger=logger;
        _session=new DesktopSession(new WindowsCredentialStore(),logger);
        _oidc=new OidcAuthenticationManager(_session,new WindowsCredentialStore("oidc-refresh"),logger);
        _api=new VmsApiClient(()=>_session.AccessToken,refreshProvider:TryRefreshCurrentAsync);
        InitializeLiveTiles();
        Loaded+=MainWindow_Loaded;
        Closing+=MainWindow_Closing;
        Deactivated+=MainWindow_Deactivated;
        SchemeCombo.SelectedIndex=0;
        PlaybackDatePicker.SelectedDate=DateTime.Today;
        PlaybackRateCombo.SelectedIndex=0;
        PtzSpeedCombo.SelectedIndex=1;
        RenderLiveGrid();
        RenderPlayback();
        RenderPtz();
    }

    private void InitializeLiveTiles()
    {
        for(var i=0;i<12;i++){LiveGridHost.RowDefinitions.Add(new RowDefinition());LiveGridHost.ColumnDefinitions.Add(new ColumnDefinition());}
        for(var i=0;i<16;i++)
        {
            var tile=new LiveTileView();
            tile.Initialize(i);
            tile.Selected+=(_,index)=>SelectLiveTile(index);
            tile.ClearRequested+=async (_,index)=>await ClearLiveTileAsync(index);
            tile.FocusRequested+=async (_,index)=>await ToggleFocusAsync(index);
            _tileViews.Add(tile);
            LiveGridHost.Children.Add(tile);
        }
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
        if(_activeProfile is not null&&(_activeProfile.Id!=p.Id||_activeProfile.SafeAddress!=p.SafeAddress))
            await EndSessionForServerChangeAsync();
        _activeProfile=p;
        DisplayNameBox.Text=p.DisplayName;SelectScheme(p.Scheme);HostBox.Text=p.Host;PortBox.Text=p.Port.ToString(System.Globalization.CultureInfo.InvariantCulture);
        _api.Configure(p);_connectionState=ServerConnectionState.Disconnected;UpdateServerState();
        await LoadAuthenticationCapabilitiesAsync();
        if(await TryRestoreCurrentSessionAsync())await RefreshServerStateAsync();
    }

    private async void NewProfile_Click(object sender,RoutedEventArgs e)
    {
        if(_activeProfile is not null)await EndSessionForServerChangeAsync();
        ProfileCombo.SelectedItem=null;_activeProfile=null;DisplayNameBox.Text="";SelectScheme("https");HostBox.Text="";PortBox.Text="443";
        _connectionState=ServerConnectionState.Disconnected;UpdateServerState();SettingsMessage.Text="Enter a new VMS server profile.";
    }

    private async void SaveProfile_Click(object sender,RoutedEventArgs e)
    {
        if(!TryBuildProfile(out var p,out var error)){SettingsMessage.Text=error;return;}
        var previous=_activeProfile;
        if(previous is not null&&!ServerProfileIdentity.SameCredentialOrigin(previous,p))
        {
            await EndSessionForServerChangeAsync();
            await _session.ForgetRememberedAsync(previous.Id);
            await _oidc.ForgetRememberedAsync(previous.Id);
            p=p with{Id=Guid.NewGuid()};
        }
        var old=previous is null?null:_profileItems.FirstOrDefault(x=>x.Id==previous.Id);
        if(old is not null)_profileItems.Remove(old);
        _profileItems.Add(p);await _profiles.SaveAsync(_profileItems);
        _activeProfile=null;ProfileCombo.SelectedItem=p;SettingsMessage.Text="Client-local server profile saved.";
    }

    private async void TestConnection_Click(object sender,RoutedEventArgs e)
    {
        if(!TryBuildProfile(out var p,out var error)){SettingsMessage.Text=error;return;}
        SettingsMessage.Text="Connecting…";
        using var probe=new VmsApiClient(()=>null);probe.Configure(p);
        SettingsMessage.Text=FriendlyConnection(await probe.TestConnectionAsync());
    }

    private async void SignIn_Click(object sender,RoutedEventArgs e)
    {
        if(_activeProfile is null||_authCapabilities?.ManualTokenLogin!=true){AuthStatusText.Text="This server does not allow access-token sign-in.";return;}
        try
        {
            SetAuthenticatingUi(true,"Signing in…");await StopLiveAsync();
            await _session.AuthenticateAsync(_activeProfile.Id,TokenBox.Password,RememberCheck.IsChecked==true,ct=>_api.GetSessionWithoutRefreshAsync(ct));
            TokenBox.Clear();LogoutButton.IsEnabled=true;AuthStatusText.Text="Signed in.";await RefreshServerStateAsync();
        }
        catch(SessionExpiredException){TokenBox.Clear();AuthStatusText.Text="Your session is invalid or expired. Sign in again.";}
        catch(Exception ex){TokenBox.Clear();_logger.LogError("auth","desktop sign-in failed",ex);AuthStatusText.Text="Unable to authenticate with the VMS server.";}
        finally{SetAuthenticatingUi(false,null);}
    }

    private async void OrganizationSignIn_Click(object sender,RoutedEventArgs e)
    {
        if(_activeProfile is null||_authCapabilities?.Oidc.Enabled!=true){AuthStatusText.Text="Organization sign-in is not available on this server.";return;}
        try
        {
            SetAuthenticatingUi(true,"Your browser is opening for sign-in…");await StopLiveAsync();
            var result=await _oidc.SignInAsync(_activeProfile,_authCapabilities.Oidc,RememberCheck.IsChecked==true,ct=>_api.GetSessionWithoutRefreshAsync(ct));
            AuthStatusText.Text=result.Remembered?"Signed in. This session can be restored securely.":"Signed in.";
            LogoutButton.IsEnabled=true;await RefreshServerStateAsync();
        }
        catch(OperationCanceledException){AuthStatusText.Text="Authentication was cancelled.";}
        catch(AuthenticationFlowException ex){AuthStatusText.Text=FriendlyAuthError(ex.Category);}
        catch(Exception ex){_logger.LogError("auth","organization sign-in failed",ex);AuthStatusText.Text="Unable to complete organization sign-in.";}
        finally{SetAuthenticatingUi(false,null);RefreshDiagnostics();}
    }

    private void CancelAuth_Click(object sender,RoutedEventArgs e){_oidc.CancelActiveAttempt();AuthStatusText.Text="Cancelling authentication…";}

    private async void Logout_Click(object sender,RoutedEventArgs e)
    {
        await DeactivatePtzAsync();
        await DeactivatePlaybackAsync();
        await DeactivateGridAsync(true);
        if(_activeProfile is not null)await _oidc.LogoutAsync(_activeProfile.Id);
        await _session.LogoutAsync();ClearAuthorizedState();_connectionState=ServerConnectionState.AuthenticationRequired;
        AuthStatusText.Text="Signed out.";UpdateServerState();
    }

    private async Task RefreshServerStateAsync()
    {
        try
        {
            _connectionState=ServerConnectionState.Connected;
            _capabilities=await _api.GetCapabilitiesAsync();
            var cameras=await _api.GetCamerasAsync();
            _authorizedCameras=cameras.ToDictionary(x=>x.Id,StringComparer.Ordinal);
            PopulateCameraTree(cameras);
            await InitializeGridCoordinatorAsync();
            await InitializePtzCoordinatorAsync();
            await InitializePlaybackCoordinatorAsync();
            EventsTab.Visibility=_capabilities.EventHistory?Visibility.Visible:Visibility.Collapsed;
            LogoutButton.IsEnabled=true;UpdateServerState();
        }
        catch(SessionExpiredException){await HandleSessionExpiredAsync();}
        catch(IncompatibleServerException){_connectionState=ServerConnectionState.Incompatible;ServerStateText.Text="The VMS server is incompatible with this client foundation.";}
        catch(Exception ex){_logger.LogError("server","server refresh failed",ex);_connectionState=ServerConnectionState.Unreachable;ServerStateText.Text="Unable to reach VMS server.";}
    }

    private void PopulateCameraTree(IEnumerable<CameraInfo> cameras)
    {
        CameraTree.Items.Clear();
        foreach(var site in CameraTreeBuilder.Build(cameras))
        {
            var root=new TreeViewItem{Header=site.SiteId,IsExpanded=true};
            foreach(var camera in site.Cameras)
                root.Items.Add(new TreeViewItem{Header=$"{camera.Name} · {(camera.Enabled?camera.DesiredState:"disabled")}",Tag=camera});
            CameraTree.Items.Add(root);
        }
    }

    private async void CameraTree_SelectedItemChanged(object sender,RoutedPropertyChangedEventArgs<object> e)
    {
        if(e.NewValue is not TreeViewItem{Tag:CameraInfo camera})
        {
            if(_playback?.Camera is not null)await _playback.ClearContextAsync();
            _selectedCamera=null;StreamRoleCombo.ItemsSource=null;PlaybackCameraText.Text="Select one authorized camera.";return;
        }
        if(_playback?.Camera is not null&&!string.Equals(_playback.Camera.Id,camera.Id,StringComparison.Ordinal))
            await _playback.ClearContextAsync();
        _selectedCamera=camera;
        LiveSelectionText.Text=$"{camera.Name} · {camera.SiteId}";
        PlaybackCameraText.Text=$"{camera.Name} · {camera.SiteId}";
        try{UpdateRoleOptions(camera.AvailableLiveRoles,LiveStreamRolePolicy.Preferred(camera,_grid?.FocusedTile is not null||_grid?.Layout.Count==1));}
        catch(InvalidOperationException){StreamRoleCombo.ItemsSource=null;LiveSelectionText.Text=$"{camera.Name} has no advertised live stream role.";}
    }

    private async void CameraTree_MouseDoubleClick(object sender,System.Windows.Input.MouseButtonEventArgs e)
    {
        if(_selectedCamera is null)return;
        await AssignSelectedCameraAsync();
        e.Handled=true;
    }

    private async void AssignSelectedCamera_Click(object sender,RoutedEventArgs e)=>await AssignSelectedCameraAsync();

    private async Task AssignSelectedCameraAsync()
    {
        if(_grid is null||_selectedCamera is null){LiveSelectionText.Text="Authenticate and select an authorized camera first.";return;}
        try
        {
            await StopPtzBestEffortAsync();
            await _grid.AssignCameraAsync(_grid.SelectedTile,_selectedCamera);
            await SaveGridSnapshotAsync();
            LiveSelectionText.Text=$"{_selectedCamera.Name} assigned to tile {_grid.SelectedTile+1}.";
            await SyncPtzForGridAsync();
        }
        catch(SessionExpiredException){await HandleSessionExpiredAsync();}
        catch(InvalidOperationException ex)
        {
            _logger.Warning("live-grid","camera assignment rejected");
            LiveSelectionText.Text=ex.Message=="Camera is already assigned to another tile."?ex.Message:"Camera cannot be assigned to the selected tile.";
        }
    }

    private async void ApplyRole_Click(object sender,RoutedEventArgs e)
    {
        if(_grid is null||StreamRoleCombo.SelectedItem is not string role){LiveSelectionText.Text="Select an assigned tile and stream role.";return;}
        try{await _grid.SwitchRoleAsync(_grid.SelectedTile,role);await SaveGridSnapshotAsync();}
        catch(SessionExpiredException){await HandleSessionExpiredAsync();}
        catch(InvalidOperationException){LiveSelectionText.Text="That stream role is not available for the active camera.";}
    }

    private async void Layout_Click(object sender,RoutedEventArgs e)
    {
        if(_grid is null||sender is not Button{Tag:string value}||!int.TryParse(value,out var count))
        {LiveSelectionText.Text="Authenticate before changing the live layout.";return;}
        try
        {
            await StopPtzBestEffortAsync();
            await _grid.SetLayoutAsync(count);
            await SaveGridSnapshotAsync();
            LiveSelectionText.Text=$"{count}-view layout selected. Grid streams prefer SUB where available.";
            await SyncPtzForGridAsync();
        }
        catch(SessionExpiredException){await HandleSessionExpiredAsync();}
    }

    private async void SelectLiveTile(int index)
    {
        if(_grid is null)return;
        try
        {
            _grid.SelectTile(index);
            var model=_grid.Tiles.FirstOrDefault(x=>x.Index==index);
            if(model?.CameraId is not null&&_authorizedCameras.TryGetValue(model.CameraId,out var camera))
                UpdateRoleOptions(camera.AvailableLiveRoles,string.IsNullOrWhiteSpace(model.ActualRole)?model.RequestedRole:model.ActualRole);
            LiveSelectionText.Text=model?.HasAssignment==true?$"{model.CameraName} · tile {index+1}":$"Tile {index+1} selected.";
            RenderLiveGrid();
            await SyncPtzForGridAsync();
        }
        catch(SessionExpiredException){await HandleSessionExpiredAsync();}
        catch(InvalidOperationException){}
    }

    private async Task ClearLiveTileAsync(int index)
    {
        if(_grid is null)return;
        if(_grid.SelectedTile==index)await StopPtzBestEffortAsync();
        await _grid.ClearTileAsync(index);
        await SaveGridSnapshotAsync();
        await SyncPtzForGridAsync();
    }

    private async Task ToggleFocusAsync(int index)
    {
        if(_grid is null)return;
        try
        {
            await StopPtzBestEffortAsync();
            if(_grid.FocusedTile==index)await _grid.ExitFocusAsync();
            else await _grid.EnterFocusAsync(index);
            await SaveGridSnapshotAsync();
            await SyncPtzForGridAsync();
        }
        catch(SessionExpiredException){await HandleSessionExpiredAsync();}
        catch(InvalidOperationException){LiveSelectionText.Text="Assign a camera before entering focus view.";}
    }

    private async Task InitializeGridCoordinatorAsync()
    {
        if(_activeProfile is null)return;
        await DeactivateGridAsync(false);
        var grid=new LiveGridCoordinator(_api,_activeProfile,_logger);
        for(var i=0;i<_tileViews.Count;i++)grid.RegisterTile(i,_tileViews[i].Renderer);
        grid.Changed+=Grid_Changed;
        _grid=grid;
        var snapshot=await _gridSettings.LoadAsync(_activeProfile.Id);
        if(snapshot is not null)await grid.RestoreAssignmentsAsync(snapshot,_authorizedCameras);
        RenderLiveGrid();
    }

    private async void Grid_Changed(object? sender,EventArgs e)
    {
        if(!Dispatcher.CheckAccess()){Dispatcher.BeginInvoke(RenderLiveGrid);return;}
        RenderLiveGrid();
        try{await SyncPtzForGridAsync();}
        catch(SessionExpiredException){await HandleSessionExpiredAsync();}
    }

    private void RenderLiveGrid()
    {
        var grid=_grid;
        var focused=grid?.FocusedTile;
        var layout=grid?.Layout??LiveGridLayout.FromCount(1);
        var visible=focused is int focus?new HashSet<int>{focus}:Enumerable.Range(0,layout.Count).ToHashSet();
        var span=focused is null?12/layout.Columns:12;
        for(var index=0;index<_tileViews.Count;index++)
        {
            var tile=_tileViews[index];
            var isVisible=visible.Contains(index);
            tile.Visibility=isVisible?Visibility.Visible:Visibility.Collapsed;
            if(isVisible)
            {
                var position=focused is null?index:0;
                var columns=focused is null?layout.Columns:1;
                Grid.SetRow(tile,(position/columns)*span);Grid.SetColumn(tile,(position%columns)*span);
                Grid.SetRowSpan(tile,span);Grid.SetColumnSpan(tile,span);
            }
            var model=grid?.Tiles.FirstOrDefault(x=>x.Index==index);
            tile.Render(model,grid?.SelectedTile==index,focused==index);
        }
        var active=grid?.ActiveTileCount??0;var connecting=grid?.ConnectingTileCount??0;var failed=grid?.FailedTileCount??0;
        GridStatusText.Text=$"{(focused is null?$"{layout.Count}-view":$"Focus tile {focused.Value+1}")} · {active} live · {connecting} connecting · {failed} failed";
        RenderPtz();
        RefreshDiagnostics();
    }

    private void UpdateRoleOptions(IEnumerable<string> roles,string? selected)
    {
        var allowed=roles.Where(x=>x is "main" or "sub" or "third").Distinct(StringComparer.OrdinalIgnoreCase).ToArray();
        StreamRoleCombo.ItemsSource=allowed;
        StreamRoleCombo.SelectedItem=allowed.Contains(selected??"",StringComparer.OrdinalIgnoreCase)?allowed.First(x=>string.Equals(x,selected,StringComparison.OrdinalIgnoreCase)):allowed.FirstOrDefault();
    }

    private async Task StopLiveAsync()
    {
        await StopPtzBestEffortAsync();
        if(_grid is not null){await _grid.StopAllAsync();await SaveGridSnapshotAsync();}
        RefreshDiagnostics();
    }

    private async Task DeactivateGridAsync(bool persist)
    {
        var grid=_grid;
        if(grid is null){RenderLiveGrid();return;}
        if(persist)await SaveGridSnapshotAsync();
        grid.Changed-=Grid_Changed;
        _grid=null;
        await grid.DisposeAsync();
        RenderLiveGrid();
    }

    private async Task SaveGridSnapshotAsync()
    {
        if(_grid is null||_activeProfile is null)return;
        try{await _gridSettings.SaveAsync(_activeProfile.Id,_grid.Snapshot());}
        catch(Exception ex){_logger.LogError("live-grid","layout persistence failed",ex);}
    }

    private async Task InitializePtzCoordinatorAsync()
    {
        await DeactivatePtzAsync();
        var ptz=new PtzCoordinator(_api,_logger);
        ptz.Changed+=Ptz_Changed;
        _ptz=ptz;
        await SyncPtzForGridAsync();
        RenderPtz();
    }

    private async Task DeactivatePtzAsync()
    {
        var ptz=_ptz;
        _ptz=null;_ptzPointerHeld=false;
        if(ptz is null){RenderPtz();return;}
        ptz.Changed-=Ptz_Changed;
        try{await ptz.DisposeAsync();}catch(Exception ex){_logger.LogError("ptz","ptz cleanup failed",ex);}
        RenderPtz();
    }

    private async Task StopPtzBestEffortAsync()
    {
        var ptz=_ptz;
        if(ptz is null)return;
        _ptzPointerHeld=false;
        try{await ptz.StopAsync();}catch(SessionExpiredException){}catch(Exception ex){_logger.LogError("ptz","best-effort PTZ stop failed",ex);}
        RenderPtz();
    }

    private async Task SyncPtzForGridAsync()
    {
        var ptz=_ptz;var grid=_grid;
        if(ptz is null)return;
        if(grid is null||MainTabs.SelectedItem!=LiveTab){await ptz.ClearAsync();RenderPtz();return;}
        var model=grid.Tiles.FirstOrDefault(x=>x.Index==grid.SelectedTile);
        var cameraId=model is {State:LiveTileState.Live,CameraId:not null}?model.CameraId:null;
        await ptz.BindAsync(grid.SelectedTile,cameraId);
        RenderPtz();
    }

    private void Ptz_Changed(object? sender,EventArgs e)
    {
        if(!Dispatcher.CheckAccess()){Dispatcher.BeginInvoke(RenderPtz);return;}
        RenderPtz();
    }

    private async void PtzMove_MouseDown(object sender,System.Windows.Input.MouseButtonEventArgs e)
    {
        if(sender is not Button button||button.Tag is not string tag||_ptz is null)return;
        var vector=tag switch{
            "0,1,0"=>(0d,1d,0d), "0,-1,0"=>(0d,-1d,0d), "-1,0,0"=>(-1d,0d,0d),
            "1,0,0"=>(1d,0d,0d), "0,0,-1"=>(0d,0d,-1d), "0,0,1"=>(0d,0d,1d),
            _=>(0d,0d,0d)};
        if(vector==(0d,0d,0d))return;
        var speed=0.65;
        if(PtzSpeedCombo.SelectedItem is ComboBoxItem{Tag:string speedTag})
            double.TryParse(speedTag,System.Globalization.NumberStyles.Float,System.Globalization.CultureInfo.InvariantCulture,out speed);
        _ptzPointerHeld=true;button.CaptureMouse();e.Handled=true;
        try{await _ptz.MoveAsync(vector.Item1,vector.Item2,vector.Item3,speed);}
        catch(SessionExpiredException){await HandleSessionExpiredAsync();}
        catch(InvalidOperationException ex){PtzStatusText.Text=ex.Message;}
        catch(VmsApiException){PtzStatusText.Text="PTZ command was rejected by the server.";}
        catch(Exception ex){_logger.LogError("ptz","ptz move failed",ex);PtzStatusText.Text="PTZ command failed.";}
    }

    private async void PtzMove_MouseUp(object sender,System.Windows.Input.MouseButtonEventArgs e)
    {
        if(!_ptzPointerHeld)return;
        _ptzPointerHeld=false;
        if(sender is Button button&&button.IsMouseCaptured)button.ReleaseMouseCapture();
        e.Handled=true;
        try{if(_ptz is not null)await _ptz.StopAsync();}
        catch(SessionExpiredException){await HandleSessionExpiredAsync();}
        catch(Exception ex){_logger.LogError("ptz","dead-man PTZ stop failed",ex);PtzStatusText.Text="Unable to confirm PTZ stop.";}
    }

    private async void PtzMove_LostMouseCapture(object sender,System.Windows.Input.MouseEventArgs e)
    {
        if(!_ptzPointerHeld)return;
        _ptzPointerHeld=false;
        try{if(_ptz is not null)await _ptz.StopAsync();}
        catch(SessionExpiredException){await HandleSessionExpiredAsync();}
        catch(Exception ex){_logger.LogError("ptz","lost-capture PTZ stop failed",ex);}
    }

    private async void PtzStop_Click(object sender,RoutedEventArgs e)=>await StopPtzBestEffortAsync();

    private async void MainWindow_Deactivated(object? sender,EventArgs e)
    {
        if(_ptz?.State==PtzState.Moving)await StopPtzBestEffortAsync();
    }

    private void RenderPtz()
    {
        var ptz=_ptz;var caps=ptz?.Capabilities;
        var activeLive=_grid?.Tiles.FirstOrDefault(x=>x.Index==_grid.SelectedTile)?.State==LiveTileState.Live&&MainTabs.SelectedItem==LiveTab;
        var ready=activeLive&&ptz?.State is (PtzState.Ready or PtzState.Moving);
        PtzUpButton.IsEnabled=PtzDownButton.IsEnabled=PtzLeftButton.IsEnabled=PtzRightButton.IsEnabled=ready&&caps?.PanTilt==true;
        PtzZoomInButton.IsEnabled=PtzZoomOutButton.IsEnabled=ready&&caps?.Zoom==true;
        PtzStopButton.IsEnabled=activeLive&&ptz?.State is (PtzState.Moving or PtzState.Stopping);
        PtzSpeedCombo.IsEnabled=ready&&(caps?.PanTilt==true||caps?.Zoom==true);
        PtzStatusText.Text=ptz is null?"PTZ unavailable":ptz.State switch{
            PtzState.Ready=>$"Ready · pan/tilt {(caps?.PanTilt==true?"yes":"no")} · optical zoom {(caps?.Zoom==true?"yes":"no")}",
            PtzState.Moving=>"Moving · release to STOP",
            PtzState.Stopping=>"Stopping…",
            PtzState.Failed=>$"PTZ unavailable · {ptz.ErrorCategory}",
            _=>"PTZ unavailable for active live tile"};
    }

    private async Task InitializePlaybackCoordinatorAsync()
    {
        if(_activeProfile is null)return;
        await DeactivatePlaybackAsync();
        PlaybackMedia.Configure(_activeProfile,()=>_session.AccessToken);
        var playback=new PlaybackCoordinator(_api,PlaybackMedia,_logger);
        playback.Changed+=Playback_Changed;
        _playback=playback;
        RenderPlayback();
    }

    private async Task DeactivatePlaybackAsync()
    {
        var playback=_playback;
        if(playback is null){RenderPlayback();return;}
        playback.Changed-=Playback_Changed;
        _playback=null;
        await playback.DisposeAsync();
        RenderPlayback();
    }

    private void Playback_Changed(object? sender,EventArgs e)
    {
        if(!Dispatcher.CheckAccess()){Dispatcher.BeginInvoke(RenderPlayback);return;}
        RenderPlayback();
    }

    private async void PlaybackDate_SelectedDateChanged(object sender,SelectionChangedEventArgs e)
    {
        if(_playback?.Day is null||PlaybackDatePicker.SelectedDate is not DateTime selected)return;
        if(_playback.Day.LocalDate!=DateOnly.FromDateTime(selected))await _playback.ClearContextAsync();
    }

    private async void MainTabs_SelectionChanged(object sender,SelectionChangedEventArgs e)
    {
        if(e.Source!=MainTabs)return;
        try
        {
            if(MainTabs.SelectedItem!=LiveTab)await StopPtzBestEffortAsync();
            if(MainTabs.SelectedItem==PlaybackTab)await StopLiveAsync();
            else if(_playback?.State is PlaybackState.Playing or PlaybackState.Paused or PlaybackState.Starting or PlaybackState.Seeking)
                await _playback.StopAsync();
        }
        catch(SessionExpiredException){await HandleSessionExpiredAsync();}
    }

    private async void PlaybackLoad_Click(object sender,RoutedEventArgs e)
    {
        if(_playback is null||_selectedCamera is null){PlaybackStatusText.Text="Authenticate and select one authorized camera first.";return;}
        if(PlaybackDatePicker.SelectedDate is not DateTime selected){PlaybackStatusText.Text="Select a recording date.";return;}
        try
        {
            await StopLiveAsync();
            await _playback.LoadDayAsync(_selectedCamera,DateOnly.FromDateTime(selected),TimeZoneInfo.Local);
        }
        catch(SessionExpiredException){await HandleSessionExpiredAsync();}
        catch(Exception ex){_logger.LogError("playback","recording day load failed",ex);PlaybackStatusText.Text="Unable to load recording availability.";}
    }

    private async void PlaybackPlay_Click(object sender,RoutedEventArgs e)
    {
        if(_playback is null)return;
        try
        {
            if(_playback.State==PlaybackState.Paused){await _playback.ResumeAsync();return;}
            var target=_playback.Position;
            if(target is null||PlaybackTimelineNormalizer.Find(_playback.Segments,target.Value) is null)
                target=_playback.Segments.Count>0?_playback.Segments[0].Start:null;
            if(target is null){PlaybackStatusText.Text="No recording available.";return;}
            await _playback.StartAsync(target.Value);
        }
        catch(SessionExpiredException){await HandleSessionExpiredAsync();}
    }

    private async void PlaybackPause_Click(object sender,RoutedEventArgs e)
    {
        if(_playback is null)return;
        try{await _playback.PauseAsync();}
        catch(SessionExpiredException){await HandleSessionExpiredAsync();}
    }

    private async void PlaybackStop_Click(object sender,RoutedEventArgs e)
    {
        if(_playback is null)return;
        try{await _playback.StopAsync();}
        catch(SessionExpiredException){await HandleSessionExpiredAsync();}
    }

    private async void PlaybackRate_SelectionChanged(object sender,SelectionChangedEventArgs e)
    {
        if(_playback is null||PlaybackRateCombo.SelectedItem is not ComboBoxItem{Tag:string tag}||!double.TryParse(tag,System.Globalization.NumberStyles.Float,System.Globalization.CultureInfo.InvariantCulture,out var rate))return;
        try{await _playback.SetRateAsync(rate);}
        catch(InvalidOperationException){PlaybackStatusText.Text="That playback speed is not supported.";}
    }

    private async void PlaybackTimeline_MouseLeftButtonDown(object sender,System.Windows.Input.MouseButtonEventArgs e)
    {
        var playback=_playback;
        if(playback?.Day is null||PlaybackTimelineCanvas.ActualWidth<=0)return;
        var fraction=Math.Clamp(e.GetPosition(PlaybackTimelineCanvas).X/PlaybackTimelineCanvas.ActualWidth,0,1);
        var target=playback.Day.StartUtc+TimeSpan.FromTicks((long)(playback.Day.Duration.Ticks*fraction));
        try
        {
            var recorded=await playback.SeekAsync(target);
            if(!recorded)PlaybackStatusText.Text="No recording at selected time.";
        }
        catch(SessionExpiredException){await HandleSessionExpiredAsync();}
    }

    private void PlaybackMarkStart_Click(object sender,RoutedEventArgs e)
    {
        if(_playback is null)return;
        try{_playback.MarkClipStart();}
        catch(InvalidOperationException){PlaybackStatusText.Text="Move playback to recorded media before marking clip start.";}
    }

    private void PlaybackMarkEnd_Click(object sender,RoutedEventArgs e)
    {
        if(_playback is null)return;
        try{_playback.MarkClipEnd();}
        catch(InvalidOperationException){PlaybackStatusText.Text="Clip end must be later and remain inside the same continuous recording span.";}
    }

    private async void PlaybackExport_Click(object sender,RoutedEventArgs e)
    {
        var playback=_playback;
        if(playback?.Camera is null||playback.ClipStart is null||playback.ClipEnd is null)return;
        var dialog=new SaveFileDialog{Filter="MP4 video (*.mp4)|*.mp4",DefaultExt=".mp4",AddExtension=true,
            FileName=$"clip-{SafeFilePart(playback.Camera.Name)}-{playback.ClipStart.Value.UtcDateTime:yyyyMMddTHHmmssZ}.mp4"};
        if(dialog.ShowDialog(this)!=true)return;
        try
        {
            await using var stream=new FileStream(dialog.FileName,FileMode.Create,FileAccess.Write,FileShare.None,81920,true);
            await playback.ExportClipAsync(stream);
            PlaybackStatusText.Text="Clip export completed.";
        }
        catch(SessionExpiredException){try{File.Delete(dialog.FileName);}catch{}await HandleSessionExpiredAsync();}
        catch(Exception ex){try{File.Delete(dialog.FileName);}catch{} _logger.LogError("playback","clip export failed",ex);PlaybackStatusText.Text="Clip export failed.";}
    }

    private void RenderPlayback()
    {
        var playback=_playback;
        PlaybackTimelineCanvas.Children.Clear();
        if(playback?.Day is not null)
        {
            var width=Math.Max(1,PlaybackTimelineCanvas.ActualWidth);
            var total=playback.Day.Duration.TotalSeconds;
            foreach(var span in playback.Segments)
            {
                var left=(span.Start-playback.Day.StartUtc).TotalSeconds/total*width;
                var spanWidth=Math.Max(2,span.Duration.TotalSeconds/total*width);
                var rect=new Rectangle{Height=34,Width=spanWidth,Fill=Brushes.SteelBlue,ToolTip=$"{playback.Day.ToDisplay(span.Start):HH:mm:ss} – {playback.Day.ToDisplay(span.End):HH:mm:ss}"};
                Canvas.SetLeft(rect,left);Canvas.SetTop(rect,7);PlaybackTimelineCanvas.Children.Add(rect);
            }
            if(playback.Position is DateTimeOffset position)
            {
                var left=(position-playback.Day.StartUtc).TotalSeconds/total*width;
                var line=new Line{X1=left,X2=left,Y1=2,Y2=46,Stroke=Brushes.OrangeRed,StrokeThickness=2};
                PlaybackTimelineCanvas.Children.Add(line);
            }
        }
        PlaybackDayStateText.Text=playback is null?"No day loaded":playback.State switch{
            PlaybackState.LoadingAvailability=>"Loading recording availability…",
            PlaybackState.Unavailable=>"No recording available",
            PlaybackState.Failed=>"Playback unavailable",
            _=>$"{playback.Segments.Count} recorded span(s) · {playback.GapCount} gap(s)"};
        PlaybackPositionText.Text=playback?.Position is DateTimeOffset pos&&playback.Day is not null
            ?$"Position: {playback.Day.ToDisplay(pos):yyyy-MM-dd HH:mm:ss}"
            :"Position: —";
        PlaybackStatusText.Text=playback is null?"Ready":FriendlyPlaybackState(playback);
        PlaybackPlayButton.IsEnabled=playback?.Segments.Count>0;
        PlaybackPauseButton.IsEnabled=playback?.State==PlaybackState.Playing;
        PlaybackStopButton.IsEnabled=playback?.State is PlaybackState.Playing or PlaybackState.Paused or PlaybackState.Starting or PlaybackState.Seeking;
        PlaybackExportButton.IsEnabled=playback?.ClipStart is not null&&playback.ClipEnd is not null;
        PlaybackClipText.Text=playback?.ClipStart is null?"Clip: not selected":
            playback.ClipEnd is null?$"Clip start: {playback.Day?.ToDisplay(playback.ClipStart.Value):HH:mm:ss}":
            $"Clip: {playback.Day?.ToDisplay(playback.ClipStart.Value):HH:mm:ss} – {playback.Day?.ToDisplay(playback.ClipEnd.Value):HH:mm:ss}";
        RefreshDiagnostics();
    }

    private static string FriendlyPlaybackState(PlaybackCoordinator playback)=>playback.ErrorCategory switch
    {
        "gap"=>"No recording at selected time.",
        "recording_removed"=>"Recording is no longer available.",
        "session_expired"=>"Session expired. Sign in again.",
        "availability_failed"=>"Unable to load recording availability.",
        "playback_failed" or "renderer_failed"=>"Playback unavailable.",
        _=>playback.State.ToString()
    };

    private static string SafeFilePart(string value)=>new(value.Where(ch=>char.IsLetterOrDigit(ch)||ch is '-' or '_').Take(60).ToArray());

    private async Task HandleSessionExpiredAsync()
    {
        await DeactivatePtzAsync();
        await DeactivatePlaybackAsync();
        await DeactivateGridAsync(true);
        if(_activeProfile is not null)await _oidc.InvalidateAsync(_activeProfile.Id);
        await _session.MarkExpiredAsync();
        ClearAuthorizedState();_connectionState=ServerConnectionState.AuthenticationRequired;
        ServerStateText.Text="Your session has expired. Sign in again.";AuthStatusText.Text="Session expired. Sign in again.";
    }

    private async void ExportDiagnostics_Click(object sender,RoutedEventArgs e)
    {
        var folder=System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),"IntelligentVMS-Diagnostics");
        var path=await DiagnosticsService.ExportAsync(BuildDiagnostics(),folder);
        DiagnosticsText.Text=$"Redacted diagnostics exported to:{Environment.NewLine}{path}";
    }

    private void MainWindow_Closing(object? sender,System.ComponentModel.CancelEventArgs e)
    {
        _oidc.CancelActiveAttempt();
        try{DeactivatePtzAsync().GetAwaiter().GetResult();}catch{}
        try{DeactivatePlaybackAsync().GetAwaiter().GetResult();}catch{}
        try{DeactivateGridAsync(true).GetAwaiter().GetResult();}catch{}
        try{PlaybackMedia.DisposeAsync().AsTask().GetAwaiter().GetResult();}catch{}
        foreach(var tile in _tileViews){try{tile.DisposeAsync().AsTask().GetAwaiter().GetResult();}catch{}}
        try{_oidc.DisposeAsync().AsTask().GetAwaiter().GetResult();}catch{}
        _api.Dispose();
    }

    private async Task EndSessionForServerChangeAsync()
    {
        await DeactivatePtzAsync();
        await DeactivatePlaybackAsync();
        await DeactivateGridAsync(true);_oidc.Deactivate();
        if(_session.State!=DesktopSessionState.SignedOut)await _session.LogoutAsync();
        _authCapabilities=null;ClearAuthorizedState();_connectionState=ServerConnectionState.Disconnected;
    }

    private async Task LoadAuthenticationCapabilitiesAsync()
    {
        try{_authCapabilities=await _api.GetAuthenticationCapabilitiesAsync();ApplyAuthenticationUx();}
        catch(Exception ex)
        {
            _authCapabilities=null;_connectionState=ServerConnectionState.Incompatible;
            _logger.LogError("auth","authentication capability negotiation failed",ex);
            AuthMethodText.Text="Authentication capability negotiation failed.";
            ManualSignInButton.Visibility=Visibility.Collapsed;TokenBox.Visibility=Visibility.Collapsed;OrganizationSignInButton.Visibility=Visibility.Collapsed;
        }
    }

    private async Task<bool> TryRestoreCurrentSessionAsync()
    {
        if(_activeProfile is null||_authCapabilities is null)return false;
        if(_authCapabilities.Oidc.Enabled)
        {
            var restored=await _oidc.TryRestoreAsync(_activeProfile,_authCapabilities.Oidc,ct=>_api.GetSessionWithoutRefreshAsync(ct));
            if(restored){AuthStatusText.Text="Remembered organization session restored.";return true;}
            if(_oidc.State==AuthenticationUxState.OfflineWithRestorableSession)
                AuthStatusText.Text="Saved sign-in is available, but the identity provider is currently unreachable.";
        }
        if(_authCapabilities.ManualTokenLogin&&await _session.TryRestoreAsync(_activeProfile.Id,ct=>_api.GetSessionWithoutRefreshAsync(ct)))
        {AuthStatusText.Text="Remembered session restored.";return true;}
        return false;
    }

    private Task<bool> TryRefreshCurrentAsync(CancellationToken cancellationToken)
    {
        if(_activeProfile is null||_authCapabilities?.Oidc.Enabled!=true)return Task.FromResult(false);
        return _oidc.TryRefreshAsync(cancellationToken);
    }

    private void ApplyAuthenticationUx()
    {
        var caps=_authCapabilities;if(caps is null)return;
        ManualSignInButton.Visibility=caps.ManualTokenLogin?Visibility.Visible:Visibility.Collapsed;
        TokenBox.Visibility=caps.ManualTokenLogin?Visibility.Visible:Visibility.Collapsed;
        OrganizationSignInButton.Visibility=caps.Oidc.Enabled?Visibility.Visible:Visibility.Collapsed;
        RememberCheck.Visibility=caps.RememberSession?Visibility.Visible:Visibility.Collapsed;
        if(caps.Oidc.Enabled)AuthMethodText.Text=caps.Oidc.Required?"Organization sign-in is required.":"Organization sign-in is available.";
        else if(caps.ManualTokenLogin)AuthMethodText.Text="Sign in with an existing VMS access token.";
        else AuthMethodText.Text=caps.Oidc.Required?"This server requires organization sign-in but is not configured for the desktop client.":"No supported authentication method is advertised.";
    }

    private void SetAuthenticatingUi(bool active,string? message)
    {
        OrganizationSignInButton.IsEnabled=!active;ManualSignInButton.IsEnabled=!active;ProfileCombo.IsEnabled=!active;
        CancelAuthButton.Visibility=active?Visibility.Visible:Visibility.Collapsed;
        if(message is not null)AuthStatusText.Text=message;
    }

    private static string FriendlyAuthError(string category)=>category switch
    {
        "cancelled"=>"Authentication was cancelled.","callback_timeout"=>"Authentication timed out. Try again.",
        "browser_launch"=>"Unable to open the system browser.","identity_unreachable"=>"Unable to contact the identity provider.",
        "state_mismatch" or "callback_replay" or "nonce_mismatch"=>"Authentication response could not be verified.",
        "oidc_unavailable" or "oidc_configuration"=>"This server is not configured for organization sign-in.",
        _=>"Authentication could not be completed."
    };

    private void ClearAuthorizedState()
    {
        _selectedCamera=null;_authorizedCameras=new Dictionary<string,CameraInfo>(StringComparer.Ordinal);
        PlaybackCameraText.Text="Select one authorized camera.";PlaybackDayStateText.Text="No day loaded";PlaybackStatusText.Text="Ready";
        CameraTree.Items.Clear();StreamRoleCombo.ItemsSource=null;_capabilities=null;
        EventsTab.Visibility=Visibility.Collapsed;LogoutButton.IsEnabled=false;RenderLiveGrid();
    }

    private bool TryBuildProfile(out ServerProfile p,out string error)
    {
        var scheme=(SchemeCombo.SelectedItem as ComboBoxItem)?.Content?.ToString()??"https";
        if(!int.TryParse(PortBox.Text,out var port))port=0;
        p=new ServerProfile(_activeProfile?.Id??Guid.NewGuid(),DisplayNameBox.Text.Trim(),scheme,HostBox.Text.Trim(),port);
        var result=ServerProfileValidator.Validate(p);error=result.Message;return result.IsValid;
    }

    private void SelectScheme(string scheme)=>SchemeCombo.SelectedIndex=scheme=="http"?1:0;
    private void UpdateServerState(){ServerStateText.Text=$"{_activeProfile?.DisplayName??"No server"} · {_connectionState}";RefreshDiagnostics();}

    private ClientDiagnostics BuildDiagnostics()
    {
        var oidcEnabled=_authCapabilities?.Oidc.Enabled==true;
        var authMode=oidcEnabled?"OIDC":_authCapabilities?.ManualTokenLogin==true?"access-token":"none";
        var authState=oidcEnabled?_oidc.State.ToString():_session.State.ToString();
        var issuerHost="not configured";
        if(oidcEnabled&&Uri.TryCreate(_authCapabilities!.Oidc.Authority,UriKind.Absolute,out var issuer))issuerHost=issuer.Host;
        var remembered=_oidc.RememberedSession||_session.RememberedSession;
        var credentialStatus=remembered?"protected remembered material present":"no remembered material";
        var grid=_grid;
        var tileStates=grid is null?"none":string.Join(",",grid.Tiles.Where(x=>x.HasAssignment)
            .Select(x=>$"{x.TileId}:{x.State}:{(string.IsNullOrWhiteSpace(x.ActualRole)?x.RequestedRole:x.ActualRole)}"));
        var playback=_playback;
        var playbackCamera=playback?.Camera is null?"none":$"{playback.Camera.Id}:{playback.Camera.Name}";
        var playbackDate=playback?.Day?.LocalDate.ToString("yyyy-MM-dd",System.Globalization.CultureInfo.InvariantCulture)??"none";
        var playbackPosition=playback?.Position?.UtcDateTime.ToString("O",System.Globalization.CultureInfo.InvariantCulture)??"none";
        return DiagnosticsService.Build(_activeProfile,_connectionState,_capabilities,grid is null?"IDLE":"GRID",
            authMode,authState,remembered,_oidc.TokenExpiry,issuerHost,_oidc.LastErrorCategory,
            oidcEnabled?OidcAuthenticationManager.CallbackMechanism:"none",credentialStatus,
            grid is null?"1-view":$"{grid.Layout.Count}-view",grid?.ActiveTileCount??0,grid?.ConnectingTileCount??0,grid?.FailedTileCount??0,tileStates,
            "WebView2-WHEP",playbackCamera,playbackDate,playback?.State.ToString()??"Idle",playback?.Rate??1.0,playbackPosition,
            playback?.Segments.Count??0,playback?.GapCount??0,"WebView2-MP4",playback?.ErrorCategory??"none",
            _ptz?.ActiveCameraId??"none",
            _ptz?.Capabilities is null?"none":$"pan_tilt={_ptz.Capabilities.PanTilt},zoom={_ptz.Capabilities.Zoom},presets={_ptz.Capabilities.Presets}",
            _ptz?.State.ToString()??"Unavailable",_ptz?.Generation??0,
            (PtzSpeedCombo.SelectedItem as ComboBoxItem)?.Content?.ToString()?.ToLowerInvariant()??"medium",
            _ptz?.ErrorCategory??"none");
    }

    private void RefreshDiagnostics()
    {
        var d=BuildDiagnostics();
        DiagnosticsText.Text=$"Version: {d.ApplicationVersion}\nOS: {d.OsVersion}\nArchitecture: {d.Architecture}\nServer: {d.ServerAddress}\nConnection: {d.ConnectionState}\nServer profile: {d.DeploymentProfile}\nMedia: {d.MediaState}\nAuthentication: {d.AuthenticationMode} / {d.AuthenticationState}\nRemembered: {d.RememberedSession}\nToken expiry: {d.TokenExpiry}\nOIDC issuer host: {d.OidcIssuerHost}\nLast auth error: {d.LastAuthErrorCategory}\nCallback: {d.CallbackMechanism}\nCredential store: {d.CredentialStoreStatus}\nLive layout: {d.LiveLayout}\nActive tiles: {d.ActiveTiles}\nConnecting tiles: {d.ConnectingTiles}\nFailed tiles: {d.FailedTiles}\nTile states: {d.LiveTileStates}\nRenderer: {d.RendererType}\nPTZ camera: {d.PtzCamera}\nPTZ capability: {d.PtzCapability}\nPTZ state: {d.PtzState}\nPTZ generation: {d.PtzGeneration}\nPTZ speed: {d.PtzSpeed}\nPTZ last error: {d.PtzErrorCategory}\nLogs: {d.LogLocation}";
    }

    private static string FriendlyConnection(ServerConnectionState s)=>s switch
    {
        ServerConnectionState.Connected=>"VMS server is reachable.",
        ServerConnectionState.AuthenticationRequired=>"VMS server is reachable; authentication is required.",
        ServerConnectionState.TlsError=>"TLS/certificate validation failed. Trust must be fixed; validation is not bypassed.",
        ServerConnectionState.Incompatible=>"Server capability contract is incompatible.",
        _=>"Unable to reach VMS server."
    };
}
