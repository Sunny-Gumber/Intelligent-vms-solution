using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Web.WebView2.Core;

namespace IntelligentVMS.Desktop;

// SemaphoreSlim is used only as an async serialization primitive; AvailableWaitHandle is never requested.
#pragma warning disable CA1001
public partial class LiveMediaView : UserControl, ILiveMediaRenderer, IAsyncDisposable
{
    private bool _initialized;
    private readonly SemaphoreSlim _gate = new(1,1);
    public string State { get; private set; } = "IDLE";
    public event EventHandler<LiveRendererStateChangedEventArgs>? StateChanged;

    public LiveMediaView()
    {
        InitializeComponent();
        Unloaded += async (_,_) => await StopAsync();
    }

    public async Task StartAsync(LiveAccessGrant grant,CancellationToken cancellationToken=default)
    {
        await _gate.WaitAsync(cancellationToken);
        try
        {
            await EnsureInitializedAsync();
            WebView.CoreWebView2.PostWebMessageAsJson(JsonSerializer.Serialize(new { action="start", webrtcUrl=grant.WebRtcUrl, accessToken=grant.AccessToken }));
            SetState("CONNECTING");
        }
        finally{_gate.Release();}
    }

    public async Task StopAsync(CancellationToken cancellationToken=default)
    {
        await _gate.WaitAsync(cancellationToken);
        try
        {
            if(_initialized) WebView.CoreWebView2.PostWebMessageAsJson("{\"action\":\"stop\"}");
            SetState("IDLE");
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
        core.WebMessageReceived+=(_,e)=>{
            try
            {
                using var doc=JsonDocument.Parse(e.WebMessageAsJson);
                if(doc.RootElement.TryGetProperty("state",out var value)) Dispatcher.Invoke(()=>SetState(value.GetString()??"UNKNOWN"));
            }
            catch{Dispatcher.Invoke(()=>SetState("FAILED"));}
        };
        var mediaFolder=Path.Combine(AppContext.BaseDirectory,"Media");
        core.SetVirtualHostNameToFolderMapping("app.intelligentvms.local",mediaFolder,CoreWebView2HostResourceAccessKind.DenyCors);
        core.Navigate("https://app.intelligentvms.local/live.html");
        _initialized=true;
    }

    private void SetState(string state)
    {
        State=state;
        StateText.Text=state switch{"LIVE"=>"LIVE","CONNECTING"=>"Connecting live view…","FAILED"=>"Live view unavailable",_=>"No live session"};
        StateOverlay.Visibility=state=="LIVE"?Visibility.Collapsed:Visibility.Visible;
        StateChanged?.Invoke(this,new LiveRendererStateChangedEventArgs(state));
    }
    public async ValueTask DisposeAsync()
    {
        await StopAsync();
        if(_initialized) WebView.Dispose();
        _gate.Dispose();
    }
}
#pragma warning restore CA1001
