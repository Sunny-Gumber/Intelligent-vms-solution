using System.Text;
using IntelligentVMS.Desktop;

internal static class PlaybackTests
{
    public static async Task RunAllAsync()
    {
        TestTimelineAndTimezone();
        await TestLifecycleAndGapSeekAsync();
        await TestRapidSeekFencingAsync();
        await TestAuthAndExportAsync();
        TestRendererUrlPolicy();
    }

    private static void TestTimelineAndTimezone()
    {
        var day=PlaybackDayWindow.Create(new DateOnly(2026,10,4),TimeZoneInfo.Utc);
        var rows=new[]{
            Span("2026-10-04T00:30:00Z","2026-10-04T01:00:00Z"),
            Span("2026-10-04T00:50:00Z","2026-10-04T01:15:00Z"),
            Span("2026-10-04T01:20:00Z","2026-10-04T02:10:00Z"),
            Span("2026-10-04T03:00:00Z","2026-10-04T02:00:00Z")};
        var normalized=PlaybackTimelineNormalizer.Normalize(rows,day);
        Assert(normalized.Count==2&&PlaybackTimelineNormalizer.GapCount(normalized)==1);
        Assert(PlaybackTimelineNormalizer.Find(normalized,DateTimeOffset.Parse("2026-10-04T01:17:00Z")) is null);

        var zone=FindEastern();
        Assert(Math.Abs(PlaybackDayWindow.Create(new DateOnly(2026,3,8),zone).Duration.TotalHours-23)<0.01);
        Assert(Math.Abs(PlaybackDayWindow.Create(new DateOnly(2026,11,1),zone).Duration.TotalHours-25)<0.01);
    }

    private static async Task TestLifecycleAndGapSeekAsync()
    {
        var provider=new FakeProvider{Spans=[
            Span("2026-10-04T10:00:00Z","2026-10-04T10:30:00Z"),
            Span("2026-10-04T10:40:00Z","2026-10-04T11:00:00Z")]};
        var renderer=new FakeRenderer();
        await using var playback=new PlaybackCoordinator(provider,renderer,new TestLogger());
        await playback.LoadDayAsync(Camera("cam-a"),new DateOnly(2026,10,4),TimeZoneInfo.Utc);
        Assert(playback.State==PlaybackState.Ready&&playback.Segments.Count==2);
        Assert(await playback.StartAsync(DateTimeOffset.Parse("2026-10-04T10:05:00Z")));
        await playback.PauseAsync();Assert(playback.State==PlaybackState.Paused);
        await playback.ResumeAsync();Assert(playback.State==PlaybackState.Playing);
        Assert(!await playback.SeekAsync(DateTimeOffset.Parse("2026-10-04T10:35:00Z")));
        Assert(playback.ErrorCategory=="gap");
        Assert(await playback.SeekAsync(DateTimeOffset.Parse("2026-10-04T10:45:00Z")));
        await ThrowsAsync<InvalidOperationException>(()=>playback.SetRateAsync(2.0));
        await playback.StopAsync();Assert(playback.State==PlaybackState.Ready&&renderer.State=="IDLE");
    }

    private static async Task TestRapidSeekFencingAsync()
    {
        var provider=new FakeProvider{Spans=[Span("2026-10-04T09:00:00Z","2026-10-04T15:00:00Z")]};
        var renderer=new DelayedRenderer();
        await using var playback=new PlaybackCoordinator(provider,renderer,new TestLogger());
        await playback.LoadDayAsync(Camera("cam-race"),new DateOnly(2026,10,4),TimeZoneInfo.Utc);
        var first=playback.SeekAsync(DateTimeOffset.Parse("2026-10-04T10:01:00Z"));
        await renderer.FirstStarted.Task.WaitAsync(TimeSpan.FromSeconds(2));
        var second=playback.SeekAsync(DateTimeOffset.Parse("2026-10-04T14:00:00Z"));
        await renderer.SecondStarted.Task.WaitAsync(TimeSpan.FromSeconds(2));
        renderer.ReleaseFirst.TrySetResult(true);
        await Task.WhenAll(first,second);
        Assert(renderer.ActiveStart==DateTimeOffset.Parse("2026-10-04T14:00:00Z"));
    }

    private static async Task TestAuthAndExportAsync()
    {
        var provider=new FakeProvider{Spans=[Span("2026-10-04T10:00:00Z","2026-10-04T10:30:00Z")]};
        var renderer=new FakeRenderer();
        await using var playback=new PlaybackCoordinator(provider,renderer,new TestLogger());
        await playback.LoadDayAsync(Camera("cam-export"),new DateOnly(2026,10,4),TimeZoneInfo.Utc);
        await playback.StartAsync(DateTimeOffset.Parse("2026-10-04T10:05:00Z"));
        renderer.Emit(DateTimeOffset.Parse("2026-10-04T10:05:00Z"));playback.MarkClipStart();
        renderer.Emit(DateTimeOffset.Parse("2026-10-04T10:10:00Z"));playback.MarkClipEnd();
        await using var output=new MemoryStream();await playback.ExportClipAsync(output);
        Assert(output.Length>0&&provider.ExportCalls==1);
        provider.Expire=true;
        await ThrowsAsync<SessionExpiredException>(()=>playback.StartAsync(DateTimeOffset.Parse("2026-10-04T10:12:00Z")));
        Assert(playback.ErrorCategory=="session_expired");
    }

    private static void TestRendererUrlPolicy()
    {
        var profile=new ServerProfile(Guid.NewGuid(),"VMS","https","vms.example.test",443);
        PlaybackMediaView.ValidatePlaybackUri(profile,new Uri("https://vms.example.test/api/v1/recordings/cameras/cam/play?start=2026-10-04T10%3A00%3A00Z&duration=60&format=mp4"));
        Throws<InvalidOperationException>(()=>PlaybackMediaView.ValidatePlaybackUri(profile,new Uri("http://vms.example.test/api/v1/recordings/cameras/cam/play?duration=60")));
        Throws<InvalidOperationException>(()=>PlaybackMediaView.ValidatePlaybackUri(profile,new Uri("https://other.example.test/api/v1/recordings/cameras/cam/play?duration=60")));
        var sensitiveKey=string.Concat("to","ken");
        Throws<InvalidOperationException>(()=>PlaybackMediaView.ValidatePlaybackUri(profile,new Uri($"https://vms.example.test/api/v1/recordings/cameras/cam/play?{sensitiveKey}=x")));
    }

    private static RecordingSpanDto Span(string start,string end)
    {
        var s=DateTimeOffset.Parse(start);var e=DateTimeOffset.Parse(end);return new(){Start=s,End=e,Duration=(e-s).TotalSeconds};
    }
    private static CameraInfo Camera(string id)=>new(){Id=id,TenantId="t",SiteId="s",Name=id,Enabled=true,DesiredState="provisioned"};
    private static TimeZoneInfo FindEastern()
    {
        foreach(var id in new[]{"Eastern Standard Time","America/New_York"})
            try{return TimeZoneInfo.FindSystemTimeZoneById(id);}catch(TimeZoneNotFoundException){}catch(InvalidTimeZoneException){}
        throw new InvalidOperationException("Timezone data unavailable.");
    }
    private static void Assert(bool value){if(!value)throw new InvalidOperationException("Playback assertion failed.");}
    private static void Throws<T>(Action action) where T:Exception{try{action();}catch(T){return;}throw new InvalidOperationException($"Expected {typeof(T).Name}.");}
    private static async Task ThrowsAsync<T>(Func<Task> action) where T:Exception{try{await action();}catch(T){return;}throw new InvalidOperationException($"Expected {typeof(T).Name}.");}

    private class FakeProvider:IPlaybackProvider
    {
        public IReadOnlyList<RecordingSpanDto> Spans{get;set;}=[];
        public bool Expire{get;set;} public int ExportCalls{get;private set;}
        public Task EnsurePlaybackAuthorizedAsync(CancellationToken cancellationToken=default){if(Expire)throw new SessionExpiredException();return Task.CompletedTask;}
        public Task<IReadOnlyList<RecordingSpanDto>> GetRecordingTimelineAsync(string cameraId,DateTimeOffset start,DateTimeOffset endTime,CancellationToken cancellationToken=default)=>Task.FromResult(Spans);
        public Uri BuildPlaybackUri(string cameraId,DateTimeOffset start,double durationSeconds)=>new($"https://vms.example.test/api/v1/recordings/cameras/{cameraId}/play?start={Uri.EscapeDataString(start.ToString("O"))}&duration={durationSeconds}&format=mp4");
        public async Task ExportClipAsync(string cameraId,DateTimeOffset start,double durationSeconds,Stream destination,CancellationToken cancellationToken=default){ExportCalls++;await destination.WriteAsync(Encoding.UTF8.GetBytes("media"),cancellationToken);}
    }

    private class FakeRenderer:IPlaybackMediaRenderer
    {
        public string State{get;protected set;}="IDLE";public Guid ActiveSession{get;protected set;} public DateTimeOffset? ActiveStart{get;protected set;}
        public event EventHandler<PlaybackRendererStateChangedEventArgs>? StateChanged;public event EventHandler<PlaybackPositionChangedEventArgs>? PositionChanged;
        public virtual Task OpenAsync(PlaybackMediaRequest request,CancellationToken cancellationToken=default){ActiveSession=request.SessionId;ActiveStart=request.Start;State="PLAYING";StateChanged?.Invoke(this,new(request.SessionId,State));PositionChanged?.Invoke(this,new(request.SessionId,request.Start));return Task.CompletedTask;}
        public Task PlayAsync(CancellationToken cancellationToken=default){State="PLAYING";StateChanged?.Invoke(this,new(ActiveSession,State));return Task.CompletedTask;}
        public Task PauseAsync(CancellationToken cancellationToken=default){State="PAUSED";StateChanged?.Invoke(this,new(ActiveSession,State));return Task.CompletedTask;}
        public Task SetRateAsync(double rate,CancellationToken cancellationToken=default)=>Task.CompletedTask;
        public Task StopAsync(CancellationToken cancellationToken=default){State="IDLE";return Task.CompletedTask;}
        public void Emit(DateTimeOffset value)=>PositionChanged?.Invoke(this,new(ActiveSession,value));
    }

    private sealed class DelayedRenderer:FakeRenderer
    {
        private int _count; private long _generation;
        public TaskCompletionSource<bool> FirstStarted{get;}=new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource<bool> SecondStarted{get;}=new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource<bool> ReleaseFirst{get;}=new(TaskCreationOptions.RunContinuationsAsynchronously);
        public override async Task OpenAsync(PlaybackMediaRequest request,CancellationToken cancellationToken=default)
        {
            var generation=Interlocked.Increment(ref _generation);
            var n=Interlocked.Increment(ref _count);
            if(n==1){FirstStarted.TrySetResult(true);await ReleaseFirst.Task;}else SecondStarted.TrySetResult(true);
            if(generation!=Volatile.Read(ref _generation))return;
            await base.OpenAsync(request,CancellationToken.None);
        }
        public new Task StopAsync(CancellationToken cancellationToken=default){Interlocked.Increment(ref _generation);return base.StopAsync(cancellationToken);}
    }
    private sealed class TestLogger:IClientLogger{public void Info(string subsystem,string message){}public void Warning(string subsystem,string message){}public void LogError(string subsystem,string message,Exception? exception=null){}}
}
