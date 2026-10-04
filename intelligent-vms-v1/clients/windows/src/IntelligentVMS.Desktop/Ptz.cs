using System.Text.Json.Serialization;

namespace IntelligentVMS.Desktop;

public enum PtzState { Unavailable, Ready, Moving, Stopping, Failed }

public sealed class PtzCapabilities
{
    [JsonPropertyName("camera_id")] public string CameraId { get; set; } = "";
    [JsonPropertyName("ptz")] public bool Ptz { get; set; }
    [JsonPropertyName("pan_tilt")] public bool PanTilt { get; set; }
    [JsonPropertyName("zoom")] public bool Zoom { get; set; }
    [JsonPropertyName("presets")] public bool Presets { get; set; }
    [JsonPropertyName("software_supported")] public bool SoftwareSupported { get; set; }
    [JsonPropertyName("hardware_verified")] public bool HardwareVerified { get; set; }
}

public sealed class PtzMoveRequestDto
{
    [JsonPropertyName("pan")] public double Pan { get; set; }
    [JsonPropertyName("tilt")] public double Tilt { get; set; }
    [JsonPropertyName("zoom")] public double Zoom { get; set; }
    [JsonPropertyName("generation")] public int Generation { get; set; }
    [JsonPropertyName("context_id")] public Guid ContextId { get; set; }
}
public sealed class PtzStopRequestDto
{
    [JsonPropertyName("generation")] public int Generation { get; set; }
    [JsonPropertyName("context_id")] public Guid ContextId { get; set; }
}
public sealed class PtzCommandAck
{
    [JsonPropertyName("camera_id")] public string CameraId { get; set; } = "";
    [JsonPropertyName("state")] public string State { get; set; } = "";
    [JsonPropertyName("generation")] public int Generation { get; set; }
}

public interface IPtzProvider
{
    Task<PtzCapabilities> GetPtzCapabilitiesAsync(string cameraId,CancellationToken cancellationToken=default);
    Task<PtzCommandAck> MovePtzAsync(string cameraId,PtzMoveRequestDto request,CancellationToken cancellationToken=default);
    Task<PtzCommandAck> StopPtzAsync(string cameraId,PtzStopRequestDto request,CancellationToken cancellationToken=default);
}

public sealed class PtzCoordinator : IAsyncDisposable
{
    private readonly IPtzProvider _provider;
    private readonly IClientLogger _logger;
    private readonly Guid _contextId=Guid.NewGuid();
    private readonly SemaphoreSlim _gate=new(1,1);
    private CancellationTokenSource? _pending;
    private string? _cameraId;
    private int _tileIndex=-1;
    private int _generation;
    private (double Pan,double Tilt,double Zoom)? _motion;
    private bool _disposed;

    public PtzState State{get;private set;}=PtzState.Unavailable;
    public PtzCapabilities? Capabilities{get;private set;}
    public string? ActiveCameraId=>_cameraId;
    public int ActiveTileIndex=>_tileIndex;
    public int Generation=>Volatile.Read(ref _generation);
    public string ErrorCategory{get;private set;}="none";
    public event EventHandler? Changed;

    public PtzCoordinator(IPtzProvider provider,IClientLogger logger){_provider=provider;_logger=logger;}

    public async Task BindAsync(int tileIndex,string? cameraId,CancellationToken cancellationToken=default)
    {
        ThrowIfDisposed();
        await _gate.WaitAsync(cancellationToken);
        try
        {
            if(_cameraId is not null && (_cameraId!=cameraId||_tileIndex!=tileIndex))
                await StopLockedAsync(CancellationToken.None,true);
            FencePending();
            _tileIndex=tileIndex;_cameraId=string.IsNullOrWhiteSpace(cameraId)?null:cameraId;
            Capabilities=null;_motion=null;ErrorCategory="none";State=PtzState.Unavailable;Notify();
            if(_cameraId is null)return;
            var generation=Interlocked.Increment(ref _generation);
            using var linked=CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            _pending=linked;
            try
            {
                var capabilities=await _provider.GetPtzCapabilitiesAsync(_cameraId,linked.Token);
                if(generation!=_generation||linked.IsCancellationRequested)return;
                if(!string.Equals(capabilities.CameraId,_cameraId,StringComparison.Ordinal))throw new InvalidOperationException("PTZ capability camera mismatch.");
                Capabilities=capabilities;
                State=capabilities.Ptz&&(capabilities.PanTilt||capabilities.Zoom)?PtzState.Ready:PtzState.Unavailable;
                _logger.Info("ptz",$"ptz_capability_loaded camera_id={Safe(_cameraId)} pan_tilt={capabilities.PanTilt} zoom={capabilities.Zoom}");
            }
            catch(OperationCanceledException) when(linked.IsCancellationRequested){}
            catch(SessionExpiredException){State=PtzState.Failed;ErrorCategory="authentication";throw;}
            catch(VmsApiException ex) when(ex.StatusCode is System.Net.HttpStatusCode.UnprocessableEntity or System.Net.HttpStatusCode.NotFound)
            {State=PtzState.Unavailable;ErrorCategory="unsupported";}
            catch(Exception ex){State=PtzState.Failed;ErrorCategory="capability";_logger.LogError("ptz","ptz capability load failed",ex);}
            finally{if(ReferenceEquals(_pending,linked))_pending=null;Notify();}
        }
        finally{_gate.Release();}
    }

    public async Task MoveAsync(double pan,double tilt,double zoom,double speed=0.65,CancellationToken cancellationToken=default)
    {
        ThrowIfDisposed();
        ValidateAxis(pan);ValidateAxis(tilt);ValidateAxis(zoom);
        if(!double.IsFinite(speed)||speed<=0||speed>1)throw new ArgumentOutOfRangeException(nameof(speed));
        if(pan==0&&tilt==0&&zoom==0)throw new ArgumentException("PTZ move vector must not be zero.");
        await _gate.WaitAsync(cancellationToken);
        try
        {
            if(_cameraId is null||Capabilities is null||State==PtzState.Unavailable)throw new InvalidOperationException("PTZ is unavailable for the active tile.");
            if((pan!=0||tilt!=0)&&!Capabilities.PanTilt)throw new InvalidOperationException("Pan/tilt is unavailable for the active camera.");
            if(zoom!=0&&!Capabilities.Zoom)throw new InvalidOperationException("Zoom is unavailable for the active camera.");
            var vector=(pan*speed,tilt*speed,zoom*speed);
            if(State==PtzState.Moving&&_motion==vector)return;
            FencePending();
            var generation=Interlocked.Increment(ref _generation);
            var cameraId=_cameraId;
            using var linked=CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            _pending=linked;
            try
            {
                var ack=await _provider.MovePtzAsync(cameraId,new PtzMoveRequestDto{Pan=vector.Item1,Tilt=vector.Item2,Zoom=vector.Item3,Generation=generation,ContextId=_contextId},linked.Token);
                if(generation!=_generation||linked.IsCancellationRequested)return;
                if(ack.Generation!=generation||ack.CameraId!=cameraId)throw new InvalidOperationException("PTZ move acknowledgement mismatch.");
                _motion=vector;State=PtzState.Moving;ErrorCategory="none";_logger.Info("ptz",$"ptz_move_started camera_id={Safe(cameraId)} generation={generation}");
            }
            catch(OperationCanceledException) when(linked.IsCancellationRequested){}
            catch
            {
                if(generation==_generation){State=PtzState.Failed;ErrorCategory="command";await StopLockedAsync(CancellationToken.None,true);}
                throw;
            }
            finally{if(ReferenceEquals(_pending,linked))_pending=null;Notify();}
        }
        finally{_gate.Release();}
    }

    public async Task StopAsync(CancellationToken cancellationToken=default)
    {
        ThrowIfDisposed();
        await _gate.WaitAsync(cancellationToken);
        try{await StopLockedAsync(cancellationToken,false);}
        finally{_gate.Release();}
    }

    private async Task StopLockedAsync(CancellationToken cancellationToken,bool bestEffort)
    {
        FencePending();
        var cameraId=_cameraId;
        _motion=null;
        if(cameraId is null){State=PtzState.Unavailable;Notify();return;}
        var generation=Interlocked.Increment(ref _generation);
        State=PtzState.Stopping;Notify();
        try
        {
            using var timeout=CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            timeout.CancelAfter(TimeSpan.FromSeconds(3));
            var ack=await _provider.StopPtzAsync(cameraId,new PtzStopRequestDto{Generation=generation,ContextId=_contextId},timeout.Token);
            if(generation==_generation&&ack.Generation==generation){State=Capabilities is null?PtzState.Unavailable:PtzState.Ready;ErrorCategory="none";}
            _logger.Info("ptz",$"ptz_stop_sent camera_id={Safe(cameraId)} generation={generation}");
        }
        catch(SessionExpiredException){State=PtzState.Failed;ErrorCategory="authentication";if(!bestEffort)throw;}
        catch(Exception ex){State=PtzState.Failed;ErrorCategory="stop";_logger.LogError("ptz","ptz stop failed",ex);if(!bestEffort)throw;}
        finally{Notify();}
    }

    public async Task ClearAsync(CancellationToken cancellationToken=default)
    {
        ThrowIfDisposed();
        await _gate.WaitAsync(cancellationToken);
        try
        {
            if(_cameraId is not null)await StopLockedAsync(cancellationToken,true);
            FencePending();_cameraId=null;_tileIndex=-1;Capabilities=null;_motion=null;State=PtzState.Unavailable;ErrorCategory="none";Notify();
        }
        finally{_gate.Release();}
    }

    private void FencePending(){Interlocked.Increment(ref _generation);_pending?.Cancel();_pending?.Dispose();_pending=null;}
    private static void ValidateAxis(double value){if(!double.IsFinite(value)||value < -1||value > 1)throw new ArgumentOutOfRangeException(nameof(value));}
    private static string Safe(string value)=>new(value.Where(c=>char.IsLetterOrDigit(c)||c is '-' or '_' or '.').Take(96).ToArray());
    private void Notify()=>Changed?.Invoke(this,EventArgs.Empty);
    private void ThrowIfDisposed(){if(_disposed)throw new ObjectDisposedException(nameof(PtzCoordinator));}
    public async ValueTask DisposeAsync()
    {
        if(_disposed)return;
        try{await ClearAsync();}catch{}
        _disposed=true;_gate.Dispose();
    }
}
