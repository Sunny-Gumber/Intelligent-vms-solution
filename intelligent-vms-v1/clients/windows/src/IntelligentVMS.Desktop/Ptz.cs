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

/// <summary>
/// Owns PTZ for exactly one active live tile. No monitor is held across network I/O:
/// a newer STOP can therefore cancel/fence a slow MOVE immediately.
/// </summary>
public sealed class PtzCoordinator:IAsyncDisposable
{
    private readonly IPtzProvider _provider;
    private readonly IClientLogger _logger;
    private readonly Guid _contextId=Guid.NewGuid();
    private readonly object _sync=new();
    private CancellationTokenSource? _pending;
    private string? _cameraId;
    private int _tileIndex=-1;
    private int _generation;
    private (double Pan,double Tilt,double Zoom)? _motion;
    private bool _disposed;

    public PtzState State{get;private set;}=PtzState.Unavailable;
    public PtzCapabilities? Capabilities{get;private set;}
    public string? ActiveCameraId{get{lock(_sync)return _cameraId;}}
    public int ActiveTileIndex{get{lock(_sync)return _tileIndex;}}
    public int Generation=>Volatile.Read(ref _generation);
    public string ErrorCategory{get;private set;}="none";
    public event EventHandler? Changed;

    public PtzCoordinator(IPtzProvider provider,IClientLogger logger){_provider=provider;_logger=logger;}

    public async Task BindAsync(int tileIndex,string? cameraId,CancellationToken cancellationToken=default)
    {
        ThrowIfDisposed();
        string? priorCamera;
        lock(_sync)
        {
            if(_cameraId==cameraId&&_tileIndex==tileIndex&&(Capabilities is not null||_pending is not null))return;
            priorCamera=_cameraId;
        }
        if(priorCamera is not null&&(priorCamera!=cameraId||ActiveTileIndex!=tileIndex))
        {
            try{await StopAsync(CancellationToken.None);}
            catch(SessionExpiredException){throw;}
            catch(Exception ex){_logger.LogError("ptz","old PTZ context stop failed during rebind",ex);}
        }

        CancellationTokenSource linked;
        int generation;
        lock(_sync)
        {
            FencePendingLocked();
            _tileIndex=tileIndex;_cameraId=string.IsNullOrWhiteSpace(cameraId)?null:cameraId;
            Capabilities=null;_motion=null;ErrorCategory="none";State=PtzState.Unavailable;
            Notify();
            if(_cameraId is null)return;
            generation=Interlocked.Increment(ref _generation);
            linked=CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            _pending=linked;
            cameraId=_cameraId;
        }
        try
        {
            var capabilities=await _provider.GetPtzCapabilitiesAsync(cameraId!,linked.Token);
            lock(_sync)
            {
                if(generation!=_generation||linked.IsCancellationRequested||_cameraId!=cameraId)return;
                if(!string.Equals(capabilities.CameraId,cameraId,StringComparison.Ordinal))
                    throw new InvalidOperationException("PTZ capability camera mismatch.");
                Capabilities=capabilities;
                State=capabilities.Ptz&&(capabilities.PanTilt||capabilities.Zoom)?PtzState.Ready:PtzState.Unavailable;
                ErrorCategory="none";
                _logger.Info("ptz",$"ptz_capability_loaded camera_id={Safe(cameraId!)} pan_tilt={capabilities.PanTilt} zoom={capabilities.Zoom}");
            }
        }
        catch(OperationCanceledException) when(linked.IsCancellationRequested){}
        catch(SessionExpiredException)
        {
            if(SetFailureIfCurrent(generation,cameraId!,"authentication"))throw;
        }
        catch(VmsApiException ex) when(ex.StatusCode is System.Net.HttpStatusCode.UnprocessableEntity or System.Net.HttpStatusCode.NotFound)
        {SetUnavailableIfCurrent(generation,cameraId!);}
        catch(Exception ex){SetFailureIfCurrent(generation,cameraId!,"capability");_logger.LogError("ptz","ptz capability load failed",ex);}
        finally{CompletePending(linked);Notify();}
    }

    public async Task MoveAsync(double pan,double tilt,double zoom,double speed=0.65,CancellationToken cancellationToken=default)
    {
        ThrowIfDisposed();ValidateAxis(pan);ValidateAxis(tilt);ValidateAxis(zoom);
        if(!double.IsFinite(speed)||speed<=0||speed>1)throw new ArgumentOutOfRangeException(nameof(speed));
        if(pan==0&&tilt==0&&zoom==0)throw new ArgumentException("PTZ move vector must not be zero.");
        string cameraId;int generation;CancellationTokenSource linked;(double,double,double) vector=(pan*speed,tilt*speed,zoom*speed);
        lock(_sync)
        {
            if(_cameraId is null||Capabilities is null||State==PtzState.Unavailable)
                throw new InvalidOperationException("PTZ is unavailable for the active tile.");
            if((pan!=0||tilt!=0)&&!Capabilities.PanTilt)throw new InvalidOperationException("Pan/tilt is unavailable for the active camera.");
            if(zoom!=0&&!Capabilities.Zoom)throw new InvalidOperationException("Zoom is unavailable for the active camera.");
            if(State==PtzState.Moving&&_motion==vector)return;
            FencePendingLocked();
            generation=Interlocked.Increment(ref _generation);cameraId=_cameraId;
            linked=CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);_pending=linked;
        }
        try
        {
            var ack=await _provider.MovePtzAsync(cameraId,new PtzMoveRequestDto{Pan=vector.Item1,Tilt=vector.Item2,Zoom=vector.Item3,Generation=generation,ContextId=_contextId},linked.Token);
            lock(_sync)
            {
                if(generation!=_generation||linked.IsCancellationRequested||_cameraId!=cameraId)return;
                if(ack.Generation!=generation||ack.CameraId!=cameraId)throw new InvalidOperationException("PTZ move acknowledgement mismatch.");
                _motion=vector;State=PtzState.Moving;ErrorCategory="none";
                _logger.Info("ptz",$"ptz_move_started camera_id={Safe(cameraId)} generation={generation}");
            }
        }
        catch(OperationCanceledException) when(linked.IsCancellationRequested){}
        catch(SessionExpiredException)
        {
            if(SetFailureIfCurrent(generation,cameraId,"authentication"))throw;
        }
        catch(Exception ex)
        {
            var current=SetFailureIfCurrent(generation,cameraId,"command");
            if(!current)return;
            _logger.LogError("ptz","ptz move failed",ex);
            await StopAfterFailedMoveAsync(cameraId);
            throw;
        }
        finally{CompletePending(linked);Notify();}
    }

    public async Task StopAsync(CancellationToken cancellationToken=default)
    {
        ThrowIfDisposed();
        string? cameraId;int generation;CancellationTokenSource linked;
        lock(_sync)
        {
            FencePendingLocked();_motion=null;cameraId=_cameraId;
            if(cameraId is null){State=PtzState.Unavailable;Notify();return;}
            generation=Interlocked.Increment(ref _generation);State=PtzState.Stopping;ErrorCategory="none";
            linked=CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            linked.CancelAfter(TimeSpan.FromSeconds(3));_pending=linked;Notify();
        }
        try
        {
            var ack=await _provider.StopPtzAsync(cameraId,new PtzStopRequestDto{Generation=generation,ContextId=_contextId},linked.Token);
            lock(_sync)
            {
                if(generation!=_generation||_cameraId!=cameraId)return;
                if(ack.Generation!=generation||ack.CameraId!=cameraId)throw new InvalidOperationException("PTZ stop acknowledgement mismatch.");
                State=Capabilities is null?PtzState.Unavailable:PtzState.Ready;ErrorCategory="none";
                _logger.Info("ptz",$"ptz_stop_sent camera_id={Safe(cameraId)} generation={generation}");
            }
        }
        catch(OperationCanceledException) when(linked.IsCancellationRequested)
        {
            lock(_sync){if(generation==_generation){State=PtzState.Failed;ErrorCategory="stop_timeout";}}
            if(cancellationToken.IsCancellationRequested)throw;
        }
        catch(SessionExpiredException){SetFailureIfCurrent(generation,cameraId,"authentication");throw;}
        catch(Exception ex){SetFailureIfCurrent(generation,cameraId,"stop");_logger.LogError("ptz","ptz stop failed",ex);throw;}
        finally{CompletePending(linked);Notify();}
    }

    private async Task StopAfterFailedMoveAsync(string cameraId)
    {
        try{await StopAsync(CancellationToken.None);}
        catch{}
        lock(_sync){if(_cameraId==cameraId){State=PtzState.Failed;ErrorCategory="command";}}
        Notify();
    }

    public async Task ClearAsync(CancellationToken cancellationToken=default)
    {
        ThrowIfDisposed();
        string? camera;lock(_sync)camera=_cameraId;
        if(camera is not null){try{await StopAsync(cancellationToken);}catch{}}
        lock(_sync)
        {
            FencePendingLocked();_cameraId=null;_tileIndex=-1;Capabilities=null;_motion=null;
            State=PtzState.Unavailable;ErrorCategory="none";Notify();
        }
    }

    private bool SetFailureIfCurrent(int generation,string cameraId,string category)
    {
        lock(_sync)
        {
            if(generation!=_generation||_cameraId!=cameraId)return false;
            State=PtzState.Failed;ErrorCategory=category;_motion=null;return true;
        }
    }
    private void SetUnavailableIfCurrent(int generation,string cameraId)
    {
        lock(_sync){if(generation==_generation&&_cameraId==cameraId){State=PtzState.Unavailable;ErrorCategory="unsupported";}}
    }
    private void FencePendingLocked(){Interlocked.Increment(ref _generation);_pending?.Cancel();_pending?.Dispose();_pending=null;}
    private void CompletePending(CancellationTokenSource source){lock(_sync){if(ReferenceEquals(_pending,source))_pending=null;}source.Dispose();}
    private static void ValidateAxis(double value){if(!double.IsFinite(value)||value < -1||value > 1)throw new ArgumentOutOfRangeException(nameof(value));}
    private static string Safe(string value)=>new(value.Where(c=>char.IsLetterOrDigit(c)||c is '-' or '_' or '.').Take(96).ToArray());
    private void Notify()=>Changed?.Invoke(this,EventArgs.Empty);
    private void ThrowIfDisposed(){if(_disposed)throw new ObjectDisposedException(nameof(PtzCoordinator));}
    public async ValueTask DisposeAsync()
    {
        if(_disposed)return;
        try{await ClearAsync();}catch{}
        _disposed=true;
    }
}
