using IntelligentVMS.Desktop;

internal static class PtzTests
{
    public static async Task RunAllAsync()
    {
        await TestCapabilitiesAndDirectionalMoveAsync();
        await TestStopPreemptsPendingMoveAsync();
        await TestCameraSwitchStopsOldCameraAsync();
        await TestStaleCapabilityIsFencedAsync();
        await TestFailureStopAndCleanupAsync();
        await TestFreshCoordinatorUsesFreshContextAsync();
    }

    private static async Task TestCapabilitiesAndDirectionalMoveAsync()
    {
        var provider=new PtzProvider();
        await using var ptz=new PtzCoordinator(provider,new PtzLogger());
        await ptz.BindAsync(2,"camera-a");
        Assert(ptz.State==PtzState.Ready&&ptz.Capabilities?.PanTilt==true&&ptz.Capabilities.Zoom);
        await ptz.MoveAsync(-1,0,0,0.65);
        Assert(ptz.State==PtzState.Moving&&provider.Moves.Count==1);
        Assert(provider.Moves[0].Camera=="camera-a"&&provider.Moves[0].Request.Pan==-0.65);
        await ptz.StopAsync();
        Assert(ptz.State==PtzState.Ready&&provider.Stops.Count==1);
        Assert(provider.Stops[0].Request.Generation>provider.Moves[0].Request.Generation);
    }

    private static async Task TestStopPreemptsPendingMoveAsync()
    {
        var provider=new PtzProvider{DelayMove=true};
        await using var ptz=new PtzCoordinator(provider,new PtzLogger());
        await ptz.BindAsync(0,"camera-a");
        var move=ptz.MoveAsync(1,0,0);
        await provider.MoveStarted.Task.WaitAsync(TimeSpan.FromSeconds(2));
        var stop=ptz.StopAsync();
        await stop.WaitAsync(TimeSpan.FromSeconds(2));
        await move.WaitAsync(TimeSpan.FromSeconds(2));
        Assert(provider.Moves.Count==1&&provider.Stops.Count==1);
        Assert(provider.Stops[0].Request.Generation>provider.Moves[0].Request.Generation);
        Assert(provider.Moves[0].Request.ContextId==provider.Stops[0].Request.ContextId);
        Assert(ptz.State==PtzState.Ready);
    }

    private static async Task TestCameraSwitchStopsOldCameraAsync()
    {
        var provider=new PtzProvider();
        await using var ptz=new PtzCoordinator(provider,new PtzLogger());
        await ptz.BindAsync(0,"camera-a");
        await ptz.MoveAsync(0,1,0);
        await ptz.BindAsync(1,"camera-b");
        Assert(provider.Stops.Any(x=>x.Camera=="camera-a"));
        Assert(ptz.ActiveCameraId=="camera-b"&&ptz.ActiveTileIndex==1&&ptz.State==PtzState.Ready);
    }

    private static async Task TestStaleCapabilityIsFencedAsync()
    {
        var provider=new PtzProvider{DelayCapabilityFor="camera-a"};
        await using var ptz=new PtzCoordinator(provider,new PtzLogger());
        var first=ptz.BindAsync(0,"camera-a");
        await provider.CapabilityStarted.Task.WaitAsync(TimeSpan.FromSeconds(2));
        var second=ptz.BindAsync(1,"camera-b");
        provider.ReleaseCapability.TrySetResult();
        await Task.WhenAll(first,second);
        Assert(ptz.ActiveCameraId=="camera-b"&&ptz.Capabilities?.CameraId=="camera-b");
        Assert(provider.Stops.Any(x=>x.Camera=="camera-a"));
    }

    private static async Task TestFailureStopAndCleanupAsync()
    {
        var provider=new PtzProvider{FailMove=true};
        await using var ptz=new PtzCoordinator(provider,new PtzLogger());
        await ptz.BindAsync(0,"camera-a");
        await ThrowsAsync<InvalidOperationException>(()=>ptz.MoveAsync(1,0,0));
        Assert(provider.Stops.Any(x=>x.Camera=="camera-a"));
        Assert(ptz.State==PtzState.Failed&&ptz.ErrorCategory=="command");
        await ptz.ClearAsync();
        Assert(ptz.ActiveCameraId is null&&ptz.State==PtzState.Unavailable);
    }

    private static async Task TestFreshCoordinatorUsesFreshContextAsync()
    {
        var provider=new PtzProvider();
        Guid first;
        await using(var one=new PtzCoordinator(provider,new PtzLogger()))
        {
            await one.BindAsync(0,"camera-a");await one.MoveAsync(1,0,0);first=provider.Moves[^1].Request.ContextId;await one.StopAsync();
        }
        await using(var two=new PtzCoordinator(provider,new PtzLogger()))
        {
            await two.BindAsync(0,"camera-a");await two.MoveAsync(1,0,0);
            Assert(provider.Moves[^1].Request.ContextId!=first);
        }
    }

    private static void Assert(bool value){if(!value)throw new InvalidOperationException("PTZ assertion failed.");}
    private static async Task ThrowsAsync<T>(Func<Task> action) where T:Exception
    {try{await action();}catch(T){return;}throw new InvalidOperationException($"Expected {typeof(T).Name}.");}

    private sealed class PtzLogger:IClientLogger
    {public void Info(string s,string m){}public void Warning(string s,string m){}public void LogError(string s,string m,Exception? e=null){}}

    private sealed class PtzProvider:IPtzProvider
    {
        public bool DelayMove,FailMove;
        public string? DelayCapabilityFor;
        public TaskCompletionSource MoveStarted{get;}=new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource CapabilityStarted{get;}=new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource ReleaseCapability{get;}=new(TaskCreationOptions.RunContinuationsAsynchronously);
        public List<(string Camera,PtzMoveRequestDto Request)> Moves{get;}=[];
        public List<(string Camera,PtzStopRequestDto Request)> Stops{get;}=[];

        public async Task<PtzCapabilities> GetPtzCapabilitiesAsync(string cameraId,CancellationToken cancellationToken=default)
        {
            if(cameraId==DelayCapabilityFor)
            {
                CapabilityStarted.TrySetResult();
                await ReleaseCapability.Task.WaitAsync(cancellationToken);
            }
            return new(){CameraId=cameraId,Ptz=true,PanTilt=true,Zoom=true,SoftwareSupported=true,HardwareVerified=false};
        }

        public async Task<PtzCommandAck> MovePtzAsync(string cameraId,PtzMoveRequestDto request,CancellationToken cancellationToken=default)
        {
            Moves.Add((cameraId,request));MoveStarted.TrySetResult();
            if(FailMove)throw new InvalidOperationException("synthetic PTZ failure");
            if(DelayMove)await Task.Delay(Timeout.InfiniteTimeSpan,cancellationToken);
            return new(){CameraId=cameraId,State="moving",Generation=request.Generation};
        }

        public Task<PtzCommandAck> StopPtzAsync(string cameraId,PtzStopRequestDto request,CancellationToken cancellationToken=default)
        {
            Stops.Add((cameraId,request));
            return Task.FromResult(new PtzCommandAck{CameraId=cameraId,State="stopped",Generation=request.Generation});
        }
    }
}
