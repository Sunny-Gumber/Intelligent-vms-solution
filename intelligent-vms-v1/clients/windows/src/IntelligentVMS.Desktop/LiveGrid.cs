using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace IntelligentVMS.Desktop;

public enum LiveTileState { Empty, Loading, Connecting, Live, Offline, Unauthorized, Failed, Stopping }

public sealed record LiveGridLayout(int Count,int Rows,int Columns)
{
    public static readonly int[] SupportedCounts=[1,4,9,16];
    public static LiveGridLayout FromCount(int count)=>count switch
    {
        1=>new(1,1,1),4=>new(4,2,2),9=>new(9,3,3),16=>new(16,4,4),
        _=>throw new ArgumentOutOfRangeException(nameof(count),"Supported live layouts are 1, 4, 9 and 16 views.")
    };
}

public sealed class LiveTileModel
{
    public int Index{get;init;}
    public string TileId=>$"tile-{Index+1}";
    public string? CameraId{get;internal set;}
    public string CameraName{get;internal set;}="";
    public string SiteId{get;internal set;}="";
    public string RequestedRole{get;internal set;}="";
    public string ActualRole{get;internal set;}="";
    public LiveTileState State{get;internal set;}=LiveTileState.Empty;
    public string ErrorCategory{get;internal set;}="none";
    private long _generation;
    public long Generation=>Volatile.Read(ref _generation);
    internal long NextGeneration()=>Interlocked.Increment(ref _generation);
    public bool HasAssignment=>!string.IsNullOrWhiteSpace(CameraId);
}

public sealed record LiveGridSnapshot(int LayoutCount,int SelectedTile,IReadOnlyList<string?> CameraIds);

public sealed class LiveGridSettingsStore
{
    private sealed class Envelope
    {
        [JsonPropertyName("profile_id")] public Guid ProfileId{get;set;}
        [JsonPropertyName("snapshot")] public LiveGridSnapshot? Snapshot{get;set;}
    }
    private readonly string _path;
    private static readonly JsonSerializerOptions Options=new(){WriteIndented=true};
    public LiveGridSettingsStore(string? path=null)=>_path=path??ClientPaths.LiveGridFile;

    public async Task SaveAsync(Guid profileId,LiveGridSnapshot snapshot,CancellationToken cancellationToken=default)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(_path)!);
        var tmp=_path+".tmp";
        await using(var stream=File.Create(tmp))
            await JsonSerializer.SerializeAsync(stream,new Envelope{ProfileId=profileId,Snapshot=snapshot},Options,cancellationToken);
        File.Move(tmp,_path,true);
    }

    public async Task<LiveGridSnapshot?> LoadAsync(Guid profileId,CancellationToken cancellationToken=default)
    {
        if(!File.Exists(_path))return null;
        try
        {
            await using var stream=File.OpenRead(_path);
            var envelope=await JsonSerializer.DeserializeAsync<Envelope>(stream,Options,cancellationToken);
            var snapshot=envelope?.ProfileId==profileId?envelope.Snapshot:null;
            if(snapshot is null||!LiveGridLayout.SupportedCounts.Contains(snapshot.LayoutCount))return null;
            if(snapshot.SelectedTile<0||snapshot.SelectedTile>=snapshot.LayoutCount)return null;
            return snapshot;
        }
        catch(JsonException){return null;}
    }
}

public static class LiveStreamRolePolicy
{
    public static string Preferred(CameraInfo camera,bool focusOrSingle)
    {
        var roles=camera.AvailableLiveRoles
            .Select(x=>x.ToLowerInvariant())
            .Where(x=>x is "main" or "sub" or "third")
            .Distinct(StringComparer.Ordinal).ToArray();
        if(roles.Length==0)throw new InvalidOperationException("Camera exposes no supported live stream role.");
        if(focusOrSingle&&roles.Contains("main"))return "main";
        if(!focusOrSingle&&roles.Contains("sub"))return "sub";
        if(roles.Contains("main"))return "main";
        return roles[0];
    }

    public static string ValidateExplicit(CameraInfo camera,string role)
    {
        role=role.ToLowerInvariant();
        if(role is not("main" or "sub" or "third")||
           !camera.AvailableLiveRoles.Contains(role,StringComparer.OrdinalIgnoreCase))
            throw new InvalidOperationException("Requested stream role is not advertised for this camera.");
        return role;
    }
}

public sealed class LiveGridCoordinator:IAsyncDisposable
{
    private sealed class TileRuntime
    {
        public required LiveTileModel Model{get;init;}
        public required ILiveMediaRenderer Renderer{get;init;}
        public required LiveSessionController Controller{get;init;}
        public CameraInfo? Camera;
        public CancellationTokenSource? Pending;
        public EventHandler<LiveRendererStateChangedEventArgs>? StateHandler;
    }

    private readonly ILiveAccessProvider _provider;
    private readonly ServerProfile _profile;
    private readonly IClientLogger _logger;
    private readonly SemaphoreSlim _connectGate;
    private readonly Dictionary<int,TileRuntime> _tiles=[];
    private int? _focusedTile;
    private bool _disposed;

    public LiveGridLayout Layout{get;private set;}=LiveGridLayout.FromCount(1);
    public int SelectedTile{get;private set;}
    public int? FocusedTile=>_focusedTile;
    public Guid ProfileId=>_profile.Id;
    public IReadOnlyList<LiveTileModel> Tiles=>_tiles.OrderBy(x=>x.Key).Select(x=>x.Value.Model).ToArray();
    public int ActiveTileCount=>_tiles.Values.Count(x=>x.Model.State==LiveTileState.Live);
    public int ConnectingTileCount=>_tiles.Values.Count(x=>x.Model.State is LiveTileState.Loading or LiveTileState.Connecting);
    public int FailedTileCount=>_tiles.Values.Count(x=>x.Model.State is LiveTileState.Failed or LiveTileState.Offline or LiveTileState.Unauthorized);
    public event EventHandler? Changed;

    public LiveGridCoordinator(ILiveAccessProvider provider,ServerProfile profile,IClientLogger logger,int maxConcurrentConnects=4)
    {
        if(maxConcurrentConnects is <1 or >16)throw new ArgumentOutOfRangeException(nameof(maxConcurrentConnects));
        _provider=provider;_profile=profile;_logger=logger;
        _connectGate=new SemaphoreSlim(maxConcurrentConnects,maxConcurrentConnects);
    }

    public void RegisterTile(int index,ILiveMediaRenderer renderer)
    {
        ThrowIfDisposed();
        if(index is <0 or >=16)throw new ArgumentOutOfRangeException(nameof(index));
        if(_tiles.ContainsKey(index))throw new InvalidOperationException("Tile is already registered.");
        var model=new LiveTileModel{Index=index};
        var runtime=new TileRuntime
        {
            Model=model,Renderer=renderer,
            Controller=new LiveSessionController(_provider,renderer,_profile,_logger)
        };
        EventHandler<LiveRendererStateChangedEventArgs> handler=(_,e)=>OnRendererState(runtime,e.State);
        runtime.StateHandler=handler;renderer.StateChanged+=handler;
        _tiles.Add(index,runtime);
        Notify();
    }

    public void SelectTile(int index){RequireVisible(index);SelectedTile=index;Notify();}

    public async Task SetLayoutAsync(int count,CancellationToken cancellationToken=default)
    {
        ThrowIfDisposed();
        Layout=LiveGridLayout.FromCount(count);_focusedTile=null;
        if(SelectedTile>=count)SelectedTile=0;
        await Task.WhenAll(_tiles.Values.Where(x=>x.Model.Index>=count)
            .Select(x=>StopRuntimeAsync(x,true,cancellationToken)));
        await Task.WhenAll(_tiles.Values.Where(x=>x.Model.Index<count&&x.Camera is not null)
            .Select(x=>EnsurePreferredRoleAsync(x,false,cancellationToken)));
        _logger.Info("live-grid",$"layout_changed views={count}");Notify();
    }

    public async Task AssignCameraAsync(int index,CameraInfo camera,CancellationToken cancellationToken=default)
    {
        RequireVisible(index);
        if(string.IsNullOrWhiteSpace(camera.Id))throw new ArgumentException("Camera ID is required.",nameof(camera));
        if(_tiles.Values.Any(x=>x.Model.Index!=index&&string.Equals(x.Model.CameraId,camera.Id,StringComparison.Ordinal)))
            throw new InvalidOperationException("Camera is already assigned to another tile.");
        var runtime=_tiles[index];
        runtime.Camera=camera;
        runtime.Model.CameraId=camera.Id;runtime.Model.CameraName=camera.Name;runtime.Model.SiteId=camera.SiteId;
        var role=LiveStreamRolePolicy.Preferred(camera,_focusedTile==index||(Layout.Count==1&&index==0));
        _logger.Info("live-grid",$"tile_assigned tile={index+1} camera_id={Safe(camera.Id)} role={role}");
        Notify();
        await StartRuntimeAsync(runtime,role,cancellationToken);
    }

    public Task SwitchRoleAsync(int index,string role,CancellationToken cancellationToken=default)
    {
        RequireVisible(index);
        var runtime=_tiles[index];
        if(runtime.Camera is null)throw new InvalidOperationException("Tile has no assigned camera.");
        role=LiveStreamRolePolicy.ValidateExplicit(runtime.Camera,role);
        _logger.Info("live-grid",$"tile_stream_role_switch tile={index+1} role={role}");
        return StartRuntimeAsync(runtime,role,cancellationToken);
    }

    public Task ClearTileAsync(int index,CancellationToken cancellationToken=default)
    {
        ThrowIfDisposed();
        return _tiles.TryGetValue(index,out var runtime)?StopRuntimeAsync(runtime,true,cancellationToken):Task.CompletedTask;
    }

    public async Task EnterFocusAsync(int index,CancellationToken cancellationToken=default)
    {
        RequireVisible(index);
        var runtime=_tiles[index];
        if(runtime.Camera is null)throw new InvalidOperationException("Select an assigned tile before focusing it.");
        _focusedTile=index;SelectedTile=index;
        await Task.WhenAll(_tiles.Values.Where(x=>x.Model.Index<Layout.Count&&x.Model.Index!=index&&x.Model.HasAssignment)
            .Select(x=>StopRuntimeAsync(x,false,cancellationToken)));
        await EnsurePreferredRoleAsync(runtime,true,cancellationToken);
        _logger.Info("live-grid",$"focus_entered tile={index+1}");Notify();
    }

    public async Task ExitFocusAsync(CancellationToken cancellationToken=default)
    {
        ThrowIfDisposed();
        if(_focusedTile is null)return;
        _focusedTile=null;
        await Task.WhenAll(_tiles.Values.Where(x=>x.Model.Index<Layout.Count&&x.Camera is not null)
            .Select(x=>EnsurePreferredRoleAsync(x,false,cancellationToken)));
        _logger.Info("live-grid","focus_exited");Notify();
    }

    public async Task StopAllAsync(CancellationToken cancellationToken=default)
    {
        ThrowIfDisposed();_focusedTile=null;
        await Task.WhenAll(_tiles.Values.Select(x=>StopRuntimeAsync(x,false,cancellationToken)));
        _logger.Info("live-grid","grid_cleanup");Notify();
    }

    public LiveGridSnapshot Snapshot()=>new(
        Layout.Count,
        Math.Clamp(SelectedTile,0,Layout.Count-1),
        Enumerable.Range(0,Layout.Count).Select(i=>_tiles.TryGetValue(i,out var t)?t.Model.CameraId:null).ToArray());

    public async Task RestoreAssignmentsAsync(LiveGridSnapshot snapshot,IReadOnlyDictionary<string,CameraInfo> authorized,CancellationToken cancellationToken=default)
    {
        await SetLayoutAsync(snapshot.LayoutCount,cancellationToken);
        SelectedTile=Math.Clamp(snapshot.SelectedTile,0,Layout.Count-1);
        for(var i=0;i<Math.Min(snapshot.CameraIds.Count,Layout.Count);i++)
        {
            var id=snapshot.CameraIds[i];
            if(string.IsNullOrWhiteSpace(id)||!authorized.TryGetValue(id,out var camera))continue;
            if(_tiles.Values.Any(x=>x.Model.Index!=i&&string.Equals(x.Model.CameraId,id,StringComparison.Ordinal)))continue;
            var runtime=_tiles[i];runtime.Camera=camera;
            runtime.Model.CameraId=camera.Id;runtime.Model.CameraName=camera.Name;runtime.Model.SiteId=camera.SiteId;
            runtime.Model.RequestedRole=LiveStreamRolePolicy.Preferred(camera,Layout.Count==1&&i==0);
            runtime.Model.ActualRole="";runtime.Model.State=LiveTileState.Empty;runtime.Model.ErrorCategory="none";
        }
        // Restore safe metadata only. Authenticated media never auto-starts after process restart.
        Notify();
    }

    private Task EnsurePreferredRoleAsync(TileRuntime runtime,bool focused,CancellationToken cancellationToken)
    {
        if(runtime.Camera is null)return Task.CompletedTask;
        var role=LiveStreamRolePolicy.Preferred(runtime.Camera,focused||(Layout.Count==1&&runtime.Model.Index==0));
        if(runtime.Model.State==LiveTileState.Live&&string.Equals(runtime.Model.ActualRole,role,StringComparison.OrdinalIgnoreCase))
            return Task.CompletedTask;
        return StartRuntimeAsync(runtime,role,cancellationToken);
    }

    private async Task StartRuntimeAsync(TileRuntime runtime,string role,CancellationToken cancellationToken)
    {
        var generation=runtime.Model.NextGeneration();
        runtime.Pending?.Cancel();
        var pending=CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);runtime.Pending=pending;
        runtime.Model.RequestedRole=role;runtime.Model.ActualRole="";runtime.Model.State=LiveTileState.Loading;runtime.Model.ErrorCategory="none";Notify();
        try
        {
            await runtime.Controller.StopAsync(CancellationToken.None);
            if(generation!=runtime.Model.Generation)return;
            await _connectGate.WaitAsync(pending.Token);
            try
            {
                if(generation!=runtime.Model.Generation)return;
                runtime.Model.State=LiveTileState.Connecting;Notify();
                await runtime.Controller.StartAsync(runtime.Camera!.Id,role,pending.Token);
            }
            finally{_connectGate.Release();}
            if(generation!=runtime.Model.Generation){await runtime.Controller.StopAsync(CancellationToken.None);return;}
            runtime.Model.ActualRole=runtime.Controller.Role??role;
            if(runtime.Renderer.State=="LIVE")runtime.Model.State=LiveTileState.Live;
            _logger.Info("live-grid",$"tile_live_start tile={runtime.Model.Index+1} camera_id={Safe(runtime.Camera!.Id)} role={role}");Notify();
        }
        catch(OperationCanceledException) when(pending.IsCancellationRequested)
        {
            if(generation==runtime.Model.Generation){runtime.Model.State=LiveTileState.Empty;runtime.Model.ErrorCategory="cancelled";Notify();}
        }
        catch(SessionExpiredException)
        {
            if(generation==runtime.Model.Generation){runtime.Model.State=LiveTileState.Unauthorized;runtime.Model.ErrorCategory="session_expired";Notify();}
            throw;
        }
        catch(VmsApiException)
        {
            if(generation==runtime.Model.Generation){runtime.Model.State=LiveTileState.Failed;runtime.Model.ErrorCategory="live_grant_rejected";Notify();}
        }
        catch(Exception)
        {
            if(generation==runtime.Model.Generation){runtime.Model.State=LiveTileState.Failed;runtime.Model.ErrorCategory="media_unavailable";Notify();}
        }
        finally
        {
            if(ReferenceEquals(runtime.Pending,pending))runtime.Pending=null;
            pending.Dispose();
        }
    }

    private async Task StopRuntimeAsync(TileRuntime runtime,bool clearAssignment,CancellationToken cancellationToken)
    {
        runtime.Model.NextGeneration();runtime.Pending?.Cancel();
        runtime.Model.State=LiveTileState.Stopping;Notify();
        try{await runtime.Controller.StopAsync(cancellationToken);}
        catch(OperationCanceledException) when(cancellationToken.IsCancellationRequested){throw;}
        catch{_logger.Warning("live-grid",$"tile_live_stop_failed tile={runtime.Model.Index+1}");}
        runtime.Model.ActualRole="";runtime.Model.State=LiveTileState.Empty;runtime.Model.ErrorCategory="none";
        if(clearAssignment)
        {
            runtime.Camera=null;runtime.Model.CameraId=null;runtime.Model.CameraName="";runtime.Model.SiteId="";runtime.Model.RequestedRole="";
        }
        _logger.Info("live-grid",$"tile_live_stop tile={runtime.Model.Index+1}");Notify();
    }

    private void OnRendererState(TileRuntime runtime,string state)
    {
        if(!runtime.Model.HasAssignment)return;
        runtime.Model.State=state switch
        {
            "LIVE"=>LiveTileState.Live,
            "CONNECTING"=>LiveTileState.Connecting,
            "FAILED"=>LiveTileState.Failed,
            "IDLE"=>runtime.Model.State==LiveTileState.Stopping?LiveTileState.Stopping:LiveTileState.Empty,
            _=>runtime.Model.State
        };
        if(state=="LIVE")runtime.Model.ErrorCategory="none";
        else if(state=="FAILED")runtime.Model.ErrorCategory="media_negotiation";
        Notify();
    }

    private void RequireVisible(int index)
    {
        ThrowIfDisposed();
        if(!_tiles.ContainsKey(index))throw new InvalidOperationException("Tile renderer is not registered.");
        if(_focusedTile is not null&&_focusedTile!=index)throw new InvalidOperationException("Tile is hidden by focus mode.");
        if(_focusedTile is null&&index>=Layout.Count)throw new InvalidOperationException("Tile is outside the current layout.");
    }
    private void Notify()=>Changed?.Invoke(this,EventArgs.Empty);
    private void ThrowIfDisposed()=>ObjectDisposedException.ThrowIf(_disposed,this);
    private static string Safe(string value)=>new(value.Where(ch=>char.IsLetterOrDigit(ch)||ch is '-' or '_' or '.').Take(80).ToArray());

    public async ValueTask DisposeAsync()
    {
        if(_disposed)return;
        await StopAllAsync();
        foreach(var runtime in _tiles.Values)
        {
            if(runtime.StateHandler is not null)runtime.Renderer.StateChanged-=runtime.StateHandler;
            runtime.Pending?.Cancel();
            await runtime.Controller.DisposeAsync();
        }
        // Do not dispose the shared gate here: a cancellation-ignoring provider may still unwind and release it.
        _disposed=true;
    }
}
