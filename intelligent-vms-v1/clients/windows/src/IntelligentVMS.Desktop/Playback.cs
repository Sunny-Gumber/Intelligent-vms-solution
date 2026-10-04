using System.IO;
using System.Globalization;

namespace IntelligentVMS.Desktop;

public enum PlaybackState
{
    Idle,
    LoadingAvailability,
    Ready,
    Starting,
    Playing,
    Paused,
    Seeking,
    Ended,
    Stopping,
    Unavailable,
    Failed
}

public sealed record RecordingSpan(DateTimeOffset Start,DateTimeOffset End)
{
    public TimeSpan Duration=>End-Start;
    public bool Contains(DateTimeOffset instant)=>instant>=Start&&instant<End;
}

public sealed record PlaybackDayWindow(DateOnly LocalDate,DateTimeOffset StartUtc,DateTimeOffset EndUtc,TimeZoneInfo DisplayTimeZone)
{
    public TimeSpan Duration=>EndUtc-StartUtc;

    public static PlaybackDayWindow Create(DateOnly date,TimeZoneInfo timeZone)
    {
        var localStart=date.ToDateTime(TimeOnly.MinValue,DateTimeKind.Unspecified);
        var localEnd=date.AddDays(1).ToDateTime(TimeOnly.MinValue,DateTimeKind.Unspecified);
        localStart=FirstValid(timeZone,localStart);
        localEnd=FirstValid(timeZone,localEnd);
        var start=TimeZoneInfo.ConvertTimeToUtc(localStart,timeZone);
        var end=TimeZoneInfo.ConvertTimeToUtc(localEnd,timeZone);
        if(end<=start)throw new InvalidOperationException("Playback day conversion produced an invalid interval.");
        return new(date,new DateTimeOffset(start),new DateTimeOffset(end),timeZone);
    }

    public DateTimeOffset ToUtc(DateTime local)
    {
        var value=DateTime.SpecifyKind(local,DateTimeKind.Unspecified);
        if(DisplayTimeZone.IsInvalidTime(value))throw new InvalidOperationException("Selected local time does not exist in this timezone.");
        if(DisplayTimeZone.IsAmbiguousTime(value))throw new InvalidOperationException("Selected local time is ambiguous in this timezone.");
        return new DateTimeOffset(TimeZoneInfo.ConvertTimeToUtc(value,DisplayTimeZone));
    }

    public DateTime ToDisplay(DateTimeOffset instant)=>TimeZoneInfo.ConvertTime(instant,DisplayTimeZone).DateTime;

    private static DateTime FirstValid(TimeZoneInfo zone,DateTime local)
    {
        for(var i=0;i<=180;i++)
        {
            var candidate=local.AddMinutes(i);
            if(!zone.IsInvalidTime(candidate))return candidate;
        }
        throw new InvalidOperationException("Playback day has no valid local boundary.");
    }
}

public static class PlaybackTimelineNormalizer
{
    public static IReadOnlyList<RecordingSpan> Normalize(IEnumerable<RecordingSpanDto> items,PlaybackDayWindow day)
    {
        var spans=new List<RecordingSpan>();
        foreach(var item in items)
        {
            if(item.End<=item.Start)continue;
            var start=item.Start<day.StartUtc?day.StartUtc:item.Start;
            var end=item.End>day.EndUtc?day.EndUtc:item.End;
            if(end<=start)continue;
            spans.Add(new(start,end));
        }
        spans.Sort((a,b)=>a.Start.CompareTo(b.Start));
        var normalized=new List<RecordingSpan>();
        foreach(var span in spans)
        {
            if(normalized.Count==0){normalized.Add(span);continue;}
            var last=normalized[^1];
            if(span.Start<=last.End)
            {
                if(span.End>last.End)normalized[^1]=last with{End=span.End};
                continue;
            }
            normalized.Add(span);
        }
        return normalized;
    }

    public static int GapCount(IReadOnlyList<RecordingSpan> spans)
    {
        var gaps=0;
        for(var i=1;i<spans.Count;i++)if(spans[i].Start>spans[i-1].End)gaps++;
        return gaps;
    }

    public static RecordingSpan? Find(IReadOnlyList<RecordingSpan> spans,DateTimeOffset target)=>
        spans.FirstOrDefault(x=>x.Contains(target));
}

public sealed record PlaybackMediaRequest(Guid SessionId,Uri MediaUri,DateTimeOffset Start,double DurationSeconds,double Rate);

public sealed class PlaybackRendererStateChangedEventArgs(Guid sessionId,string state):EventArgs
{
    public Guid SessionId{get;}=sessionId;
    public string State{get;}=state;
}
public sealed class PlaybackPositionChangedEventArgs(Guid sessionId,DateTimeOffset position):EventArgs
{
    public Guid SessionId{get;}=sessionId;
    public DateTimeOffset Position{get;}=position;
}

public interface IPlaybackMediaRenderer
{
    string State{get;}
    event EventHandler<PlaybackRendererStateChangedEventArgs>? StateChanged;
    event EventHandler<PlaybackPositionChangedEventArgs>? PositionChanged;
    Task OpenAsync(PlaybackMediaRequest request,CancellationToken cancellationToken=default);
    Task PlayAsync(CancellationToken cancellationToken=default);
    Task PauseAsync(CancellationToken cancellationToken=default);
    Task SetRateAsync(double rate,CancellationToken cancellationToken=default);
    Task StopAsync(CancellationToken cancellationToken=default);
}

public interface IPlaybackProvider
{
    Task EnsurePlaybackAuthorizedAsync(CancellationToken cancellationToken=default);
    Task<IReadOnlyList<RecordingSpanDto>> GetRecordingTimelineAsync(string cameraId,DateTimeOffset start,DateTimeOffset endTime,CancellationToken cancellationToken=default);
    Uri BuildPlaybackUri(string cameraId,DateTimeOffset start,double durationSeconds);
    Task ExportClipAsync(string cameraId,DateTimeOffset start,double durationSeconds,Stream destination,CancellationToken cancellationToken=default);
}

public sealed class PlaybackCoordinator:IAsyncDisposable
{
    private readonly IPlaybackProvider _provider;
    private readonly IPlaybackMediaRenderer _renderer;
    private readonly IClientLogger _logger;
    private CancellationTokenSource? _pending;
    private long _generation;
    private Guid? _activeSession;
    private bool _disposed;

    public PlaybackState State{get;private set;}=PlaybackState.Idle;
    public CameraInfo? Camera{get;private set;}
    public PlaybackDayWindow? Day{get;private set;}
    public IReadOnlyList<RecordingSpan> Segments{get;private set;}=[];
    public DateTimeOffset? Position{get;private set;}
    public double Rate{get;private set;}=1.0;
    public string ErrorCategory{get;private set;}="none";
    public DateTimeOffset? ClipStart{get;private set;}
    public DateTimeOffset? ClipEnd{get;private set;}
    public IReadOnlyList<double> SupportedRates{get;}=[1.0];
    public int GapCount=>PlaybackTimelineNormalizer.GapCount(Segments);
    public event EventHandler? Changed;

    public PlaybackCoordinator(IPlaybackProvider provider,IPlaybackMediaRenderer renderer,IClientLogger logger)
    {
        _provider=provider;_renderer=renderer;_logger=logger;
        renderer.StateChanged+=Renderer_StateChanged;
        renderer.PositionChanged+=Renderer_PositionChanged;
    }

    public async Task LoadDayAsync(CameraInfo camera,DateOnly date,TimeZoneInfo displayTimeZone,CancellationToken cancellationToken=default)
    {
        ThrowIfDisposed();
        var generation=NextGeneration();
        var pending=ReplacePending(cancellationToken);
        await StopRendererQuietlyAsync();
        Camera=camera;Day=PlaybackDayWindow.Create(date,displayTimeZone);Segments=[];Position=null;ClipStart=null;ClipEnd=null;
        State=PlaybackState.LoadingAvailability;ErrorCategory="none";Notify();
        try
        {
            var rows=await _provider.GetRecordingTimelineAsync(camera.Id,Day.StartUtc,Day.EndUtc,pending.Token);
            if(generation!=Generation)return;
            Segments=PlaybackTimelineNormalizer.Normalize(rows,Day);
            State=Segments.Count==0?PlaybackState.Unavailable:PlaybackState.Ready;
            Position=Segments.Count==0?null:Segments[0].Start;
            ErrorCategory=Segments.Count==0?"no_recording":"none";
            _logger.Info("playback",$"playback_day_loaded camera_id={Safe(camera.Id)} segments={Segments.Count} gaps={GapCount}");
            Notify();
        }
        catch(OperationCanceledException) when(pending.IsCancellationRequested){}
        catch(SessionExpiredException)
        {
            if(generation==Generation){State=PlaybackState.Failed;ErrorCategory="session_expired";Notify();}
            throw;
        }
        catch(VmsApiException ex) when(ex.StatusCode==System.Net.HttpStatusCode.NotFound)
        {
            if(generation==Generation){Segments=[];Position=null;State=PlaybackState.Unavailable;ErrorCategory="no_recording";Notify();}
        }
        catch(Exception)
        {
            if(generation==Generation){State=PlaybackState.Failed;ErrorCategory="availability_failed";Notify();}
        }
        finally{ReleasePending(pending);}
    }

    public async Task<bool> StartAsync(DateTimeOffset target,CancellationToken cancellationToken=default)=>
        await OpenAtAsync(target,PlaybackState.Starting,cancellationToken);

    public async Task<bool> SeekAsync(DateTimeOffset target,CancellationToken cancellationToken=default)
    {
        ThrowIfDisposed();
        if(PlaybackTimelineNormalizer.Find(Segments,target) is null)
        {
            NextGeneration();CancelPending();
            await StopRendererQuietlyAsync();
            Position=target;State=Segments.Count==0?PlaybackState.Unavailable:PlaybackState.Ready;ErrorCategory="gap";Notify();
            _logger.Info("playback","playback_seek_gap");
            return false;
        }
        return await OpenAtAsync(target,PlaybackState.Seeking,cancellationToken);
    }

    private async Task<bool> OpenAtAsync(DateTimeOffset target,PlaybackState transition,CancellationToken cancellationToken)
    {
        ThrowIfDisposed();
        if(Camera is null||Day is null)throw new InvalidOperationException("Select a camera and load recording availability first.");
        var span=PlaybackTimelineNormalizer.Find(Segments,target);
        if(span is null){Position=target;State=PlaybackState.Ready;ErrorCategory="gap";Notify();return false;}
        var generation=NextGeneration();
        var pending=ReplacePending(cancellationToken);
        var duration=Math.Min(14400.0,Math.Max(0.001,(span.End-target).TotalSeconds));
        var sessionId=Guid.NewGuid();
        State=transition;ErrorCategory="none";Position=target;Notify();
        try
        {
            await _provider.EnsurePlaybackAuthorizedAsync(pending.Token);
            if(generation!=Generation)return false;
            await _renderer.StopAsync(CancellationToken.None);
            if(generation!=Generation)return false;
            var uri=_provider.BuildPlaybackUri(Camera.Id,target,duration);
            _activeSession=sessionId;
            await _renderer.OpenAsync(new(sessionId,uri,target,duration,Rate),pending.Token);
            if(generation!=Generation||_activeSession!=sessionId)return false;
            State=PlaybackState.Playing;ErrorCategory="none";Notify();
            _logger.Info("playback",$"playback_start camera_id={Safe(Camera.Id)}");
            return true;
        }
        catch(OperationCanceledException) when(pending.IsCancellationRequested){return false;}
        catch(SessionExpiredException){if(generation==Generation){State=PlaybackState.Failed;ErrorCategory="session_expired";Notify();}throw;}
        catch(VmsApiException ex) when(ex.StatusCode==System.Net.HttpStatusCode.NotFound)
        {
            if(generation==Generation){State=PlaybackState.Unavailable;ErrorCategory="recording_removed";Notify();}
            return false;
        }
        catch(Exception)
        {
            if(generation==Generation){State=PlaybackState.Failed;ErrorCategory="playback_failed";Notify();}
            return false;
        }
        finally{ReleasePending(pending);}
    }

    public async Task PauseAsync(CancellationToken cancellationToken=default)
    {
        ThrowIfDisposed();if(State!=PlaybackState.Playing)return;
        await _renderer.PauseAsync(cancellationToken);State=PlaybackState.Paused;Notify();_logger.Info("playback","playback_pause");
    }

    public async Task ResumeAsync(CancellationToken cancellationToken=default)
    {
        ThrowIfDisposed();if(State!=PlaybackState.Paused)return;
        await _renderer.PlayAsync(cancellationToken);State=PlaybackState.Playing;Notify();_logger.Info("playback","playback_resume");
    }

    public async Task SetRateAsync(double rate,CancellationToken cancellationToken=default)
    {
        ThrowIfDisposed();
        if(!SupportedRates.Contains(rate))throw new InvalidOperationException("Playback rate is not supported by this foundation.");
        await _renderer.SetRateAsync(rate,cancellationToken);Rate=rate;Notify();
    }

    public async Task StopAsync(CancellationToken cancellationToken=default)
    {
        ThrowIfDisposed();
        NextGeneration();CancelPending();State=PlaybackState.Stopping;Notify();
        await _renderer.StopAsync(cancellationToken);
        _activeSession=null;State=Segments.Count==0?(Camera is null?PlaybackState.Idle:PlaybackState.Unavailable):PlaybackState.Ready;
        ErrorCategory="none";Notify();_logger.Info("playback","playback_stop");
    }

    public void MarkClipStart()
    {
        ThrowIfDisposed();if(Position is null||PlaybackTimelineNormalizer.Find(Segments,Position.Value) is null)throw new InvalidOperationException("Playback position is not inside recorded media.");
        ClipStart=Position;ClipEnd=null;Notify();
    }

    public void MarkClipEnd()
    {
        ThrowIfDisposed();if(Position is null||ClipStart is null)throw new InvalidOperationException("Mark clip start first.");
        if(Position<=ClipStart)throw new InvalidOperationException("Clip end must be after clip start.");
        var startSpan=PlaybackTimelineNormalizer.Find(Segments,ClipStart.Value);
        var endSpan=PlaybackTimelineNormalizer.Find(Segments,Position.Value.AddTicks(-1));
        if(startSpan is null||endSpan is null||startSpan!=endSpan)throw new InvalidOperationException("Clip range must remain within one continuous recording span.");
        ClipEnd=Position;Notify();
    }

    public async Task ExportClipAsync(Stream destination,CancellationToken cancellationToken=default)
    {
        ThrowIfDisposed();
        if(Camera is null||ClipStart is null||ClipEnd is null||ClipEnd<=ClipStart)throw new InvalidOperationException("Select a valid clip range first.");
        var startSpan=PlaybackTimelineNormalizer.Find(Segments,ClipStart.Value);
        var endSpan=PlaybackTimelineNormalizer.Find(Segments,ClipEnd.Value.AddTicks(-1));
        if(startSpan is null||endSpan is null||startSpan!=endSpan)throw new InvalidOperationException("Clip range crosses a recording gap.");
        await _provider.ExportClipAsync(Camera.Id,ClipStart.Value,(ClipEnd.Value-ClipStart.Value).TotalSeconds,destination,cancellationToken);
        _logger.Info("playback",$"playback_export_requested camera_id={Safe(Camera.Id)}");
    }

    private void Renderer_StateChanged(object? sender,PlaybackRendererStateChangedEventArgs e)
    {
        if(_activeSession!=e.SessionId)return;
        State=e.State switch
        {
            "PLAYING"=>PlaybackState.Playing,
            "PAUSED"=>PlaybackState.Paused,
            "ENDED"=>PlaybackState.Ended,
            "FAILED"=>PlaybackState.Failed,
            _=>State
        };
        if(e.State=="FAILED")ErrorCategory="renderer_failed";
        Notify();
    }

    private void Renderer_PositionChanged(object? sender,PlaybackPositionChangedEventArgs e)
    {
        if(_activeSession!=e.SessionId)return;
        Position=e.Position;Notify();
    }

    private long Generation=>Volatile.Read(ref _generation);
    private long NextGeneration()=>Interlocked.Increment(ref _generation);
    private CancellationTokenSource ReplacePending(CancellationToken cancellationToken)
    {
        CancelPending();
        var pending=CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);_pending=pending;return pending;
    }
    private void CancelPending()=>_pending?.Cancel();
    private void ReleasePending(CancellationTokenSource pending){if(ReferenceEquals(_pending,pending))_pending=null;pending.Dispose();}
    private async Task StopRendererQuietlyAsync(){try{await _renderer.StopAsync(CancellationToken.None);}catch{} _activeSession=null;}
    private void Notify()=>Changed?.Invoke(this,EventArgs.Empty);
    private void ThrowIfDisposed()=>ObjectDisposedException.ThrowIf(_disposed,this);
    private static string Safe(string value)=>new(value.Where(ch=>char.IsLetterOrDigit(ch)||ch is '-' or '_' or '.').Take(80).ToArray());

    public async ValueTask DisposeAsync()
    {
        if(_disposed)return;
        NextGeneration();CancelPending();await StopRendererQuietlyAsync();
        _renderer.StateChanged-=Renderer_StateChanged;_renderer.PositionChanged-=Renderer_PositionChanged;
        _disposed=true;
    }
}
