using System.IO;
using System.Net.Http.Headers;
using System.Text.Json;
using System.Windows.Controls;
using Microsoft.Web.WebView2.Core;

namespace IntelligentVMS.Desktop;

#pragma warning disable CA1001
public partial class PlaybackMediaView:UserControl,IPlaybackMediaRenderer,IAsyncDisposable
{
    private readonly SemaphoreSlim _gate=new(1,1);
    private bool _initialized;
    private ServerProfile? _profile;
    private Func<string?>? _tokenProvider;
    private Guid _sessionId;
    public string State{get;private set;}="IDLE";
    public event EventHandler<PlaybackRendererStateChangedEventArgs>? StateChanged;
    public event EventHandler<PlaybackPositionChangedEventArgs>? PositionChanged;

    public PlaybackMediaView(){InitializeComponent();}

    public void Configure(ServerProfile profile,Func<string?> tokenProvider)
    {
        _profile=profile;_tokenProvider=tokenProvider;
    }

    public async Task OpenAsync(PlaybackMediaRequest request,CancellationToken cancellationToken=default)
    {
        await _gate.WaitAsync(cancellationToken);
        try
        {
            if(_profile is null||_tokenProvider is null)throw new InvalidOperationException("Playback renderer is not configured.");
            ValidatePlaybackUri(_profile,request.MediaUri);
            await EnsureInitializedAsync();
            _sessionId=request.SessionId;
            WebView.CoreWebView2.PostWebMessageAsJson(JsonSerializer.Serialize(new{
                action="open",sessionId=request.SessionId,mediaUrl=request.MediaUri.ToString(),
                startUtc=request.Start.UtcDateTime.ToString("O"),rate=request.Rate
            }));
            SetState(request.SessionId,"STARTING");
        }
        finally{_gate.Release();}
    }

    public Task PlayAsync(CancellationToken cancellationToken=default)=>CommandAsync("play",cancellationToken);
    public Task PauseAsync(CancellationToken cancellationToken=default)=>CommandAsync("pause",cancellationToken);
    public Task SetRateAsync(double rate,CancellationToken cancellationToken=default)=>
        CommandAsync("rate",cancellationToken,new{rate});
    public Task StopAsync(CancellationToken cancellationToken=default)=>CommandAsync("stop",cancellationToken);

    private async Task CommandAsync(string action,CancellationToken cancellationToken,object? extra=null)
    {
        await _gate.WaitAsync(cancellationToken);
        try
        {
            if(!_initialized){if(action=="stop")SetState(_sessionId,"IDLE");return;}
            var payload=extra is null?new{action,sessionId=_sessionId}:new{action,sessionId=_sessionId,rate=extra.GetType().GetProperty("rate")?.GetValue(extra)};
            WebView.CoreWebView2.PostWebMessageAsJson(JsonSerializer.Serialize(payload));
            if(action=="stop")SetState(_sessionId,"IDLE");
        }
        finally{_gate.Release();}
    }

    private async Task EnsureInitializedAsync()
    {
        if(_initialized)return;
        var environment=await CoreWebView2Environment.CreateAsync(null,ClientPaths.WebView2CacheDirectory);
        await WebView.EnsureCoreWebView2Async(environment);
        var core=WebView.CoreWebView2;
        core.Settings.AreDevToolsEnabled=false;
        core.Settings.AreDefaultContextMenusEnabled=false;
        core.Settings.AreBrowserAcceleratorKeysEnabled=false;
        core.Settings.IsStatusBarEnabled=false;
        core.Settings.IsWebMessageEnabled=true;
        core.Settings.AreHostObjectsAllowed=false;
        core.NewWindowRequested+=(_,e)=>e.Handled=true;
        core.PermissionRequested+=(_,e)=>e.State=CoreWebView2PermissionState.Deny;
        core.NavigationStarting+=(_,e)=>{
            if(!Uri.TryCreate(e.Uri,UriKind.Absolute,out var uri)||uri.Scheme!="https"||uri.Host!="app.intelligentvms.local")e.Cancel=true;
        };
        core.AddWebResourceRequestedFilter("*",CoreWebView2WebResourceContext.Media);
        core.WebResourceRequested+=Core_WebResourceRequested;
        core.WebMessageReceived+=Core_WebMessageReceived;
        var mediaFolder=Path.Combine(AppContext.BaseDirectory,"Media");
        core.SetVirtualHostNameToFolderMapping("app.intelligentvms.local",mediaFolder,CoreWebView2HostResourceAccessKind.DenyCors);
        core.Navigate("https://app.intelligentvms.local/playback.html");
        _initialized=true;
    }

    private void Core_WebResourceRequested(object? sender,CoreWebView2WebResourceRequestedEventArgs e)
    {
        try
        {
            if(_profile is null||_tokenProvider is null||!Uri.TryCreate(e.Request.Uri,UriKind.Absolute,out var uri))return;
            if(!SameOrigin(_profile.ApiBaseAddress,uri))return;
            if(!uri.AbsolutePath.StartsWith("/api/v1/recordings/cameras/",StringComparison.Ordinal)||
               !uri.AbsolutePath.EndsWith("/play",StringComparison.Ordinal))return;
            var token=_tokenProvider();
            if(string.IsNullOrWhiteSpace(token))return;
            e.Request.Headers.SetHeader("Authorization",new AuthenticationHeaderValue("Bearer",token).ToString());
        }
        catch{}
    }

    private void Core_WebMessageReceived(object? sender,CoreWebView2WebMessageReceivedEventArgs e)
    {
        try
        {
            using var doc=JsonDocument.Parse(e.WebMessageAsJson);
            var root=doc.RootElement;
            if(!root.TryGetProperty("sessionId",out var sid)||!Guid.TryParse(sid.GetString(),out var session)||session!=_sessionId)return;
            if(root.TryGetProperty("state",out var state))
                Dispatcher.Invoke(()=>SetState(session,state.GetString()??"UNKNOWN"));
            if(root.TryGetProperty("positionUtc",out var position)&&DateTimeOffset.TryParse(position.GetString(),out var parsed))
                Dispatcher.Invoke(()=>PositionChanged?.Invoke(this,new(session,parsed)));
        }
        catch{Dispatcher.Invoke(()=>SetState(_sessionId,"FAILED"));}
    }

    private void SetState(Guid session,string state)
    {
        State=state;
        StateText.Text=state switch{
            "PLAYING"=>"Playing","PAUSED"=>"Paused","ENDED"=>"Playback ended","FAILED"=>"Playback unavailable",
            "STARTING"=>"Starting playback…",_=>"No playback session"};
        StateOverlay.Visibility=state=="PLAYING"||state=="PAUSED"?System.Windows.Visibility.Collapsed:System.Windows.Visibility.Visible;
        StateChanged?.Invoke(this,new(session,state));
    }

    public static void ValidatePlaybackUri(ServerProfile profile,Uri uri)
    {
        if(!SameOrigin(profile.ApiBaseAddress,uri))throw new InvalidOperationException("Playback resource origin does not match the active VMS server.");
        if(uri.UserInfo.Length>0||uri.Fragment.Length>0)throw new InvalidOperationException("Playback resource URL is unsafe.");
        if(!uri.AbsolutePath.StartsWith("/api/v1/recordings/cameras/",StringComparison.Ordinal)||!uri.AbsolutePath.EndsWith("/play",StringComparison.Ordinal))
            throw new InvalidOperationException("Playback resource path is invalid.");
        if(profile.Scheme=="https"&&uri.Scheme!="https")throw new InvalidOperationException("Playback resource attempted a TLS downgrade.");
        if(uri.Query.Contains("token=",StringComparison.OrdinalIgnoreCase)||
           uri.Query.Contains("access_token",StringComparison.OrdinalIgnoreCase)||
           uri.Query.Contains("refresh_token",StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("Playback resource URL must not carry tokens.");
    }

    private static bool SameOrigin(Uri left,Uri right)=>string.Equals(left.Scheme,right.Scheme,StringComparison.OrdinalIgnoreCase)&&
        string.Equals(left.Host,right.Host,StringComparison.OrdinalIgnoreCase)&&left.Port==right.Port;

    public async ValueTask DisposeAsync()
    {
        try{await StopAsync();}catch{}
        if(_initialized)
        {
            WebView.CoreWebView2.WebResourceRequested-=Core_WebResourceRequested;
            WebView.CoreWebView2.WebMessageReceived-=Core_WebMessageReceived;
            WebView.Dispose();
        }
        _gate.Dispose();GC.SuppressFinalize(this);
    }
}
#pragma warning restore CA1001
