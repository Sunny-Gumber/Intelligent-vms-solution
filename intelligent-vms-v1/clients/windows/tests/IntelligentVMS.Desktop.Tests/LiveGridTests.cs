using System.Net;
using IntelligentVMS.Desktop;

internal static class LiveGridTests
{
    public static async Task RunAllAsync()
    {
        TestLayouts();
        TestRolePolicy();
        await TestGridSessionsAndDuplicatesAsync();
        await TestLayoutShrinkCleanupAsync();
        await TestFocusMainSubRoundTripAsync();
        await TestFailureIsolationAsync();
        await TestRapidReplacementFencingAsync();
        await TestPersistenceAndAuthorizationRestoreAsync();
        await TestStopAllAndRepeatedLifecycleAsync();
    }

    private static void TestLayouts()
    {
        var one=LiveGridLayout.FromCount(1);var four=LiveGridLayout.FromCount(4);
        var nine=LiveGridLayout.FromCount(9);var sixteen=LiveGridLayout.FromCount(16);
        Assert(one.Rows==1&&one.Columns==1&&four.Rows==2&&four.Columns==2);
        Assert(nine.Rows==3&&nine.Columns==3&&sixteen.Rows==4&&sixteen.Columns==4);
        Throws<ArgumentOutOfRangeException>(()=>LiveGridLayout.FromCount(6));
    }

    private static void TestRolePolicy()
    {
        var both=Camera("both",["main","sub"]);
        var main=Camera("main",["main"]);
        var third=Camera("third",["third"]);
        Assert(LiveStreamRolePolicy.Preferred(both,false)=="sub");
        Assert(LiveStreamRolePolicy.Preferred(both,true)=="main");
        Assert(LiveStreamRolePolicy.Preferred(main,false)=="main");
        Assert(LiveStreamRolePolicy.Preferred(third,false)=="third");
        Throws<InvalidOperationException>(()=>LiveStreamRolePolicy.ValidateExplicit(main,"sub"));
    }

    private static async Task TestGridSessionsAndDuplicatesAsync()
    {
        var provider=new GridProvider();var renderers=Renderers();
        await using var grid=Grid(provider,renderers);
        await grid.SetLayoutAsync(16);
        var tasks=Enumerable.Range(0,16).Select(i=>grid.AssignCameraAsync(i,Camera($"c{i}",["main","sub"])));
        await Task.WhenAll(tasks);
        Assert(grid.Layout.Count==16&&grid.ActiveTileCount==16);
        Assert(grid.Tiles.All(x=>x.ActualRole=="sub"));
        Assert(renderers.All(x=>x.Starts==1));
        await ThrowsAsync<InvalidOperationException>(()=>grid.AssignCameraAsync(1,Camera("c0",["main","sub"])));
    }

    private static async Task TestLayoutShrinkCleanupAsync()
    {
        var provider=new GridProvider();var renderers=Renderers();
        await using var grid=Grid(provider,renderers);
        await grid.SetLayoutAsync(4);
        for(var i=0;i<4;i++)await grid.AssignCameraAsync(i,Camera($"shrink-{i}",["main","sub"]));
        await grid.SetLayoutAsync(1);
        Assert(grid.Layout.Count==1&&grid.Tiles[0].HasAssignment&&grid.Tiles[0].ActualRole=="main");
        Assert(grid.Tiles.Skip(1).Take(3).All(x=>!x.HasAssignment&&x.State==LiveTileState.Empty));
        Assert(renderers.Skip(1).Take(3).All(x=>x.Stops>0));
    }

    private static async Task TestFocusMainSubRoundTripAsync()
    {
        var provider=new GridProvider();var renderers=Renderers();
        await using var grid=Grid(provider,renderers);
        await grid.SetLayoutAsync(4);
        await grid.AssignCameraAsync(0,Camera("focus-a",["main","sub"]));
        await grid.AssignCameraAsync(1,Camera("focus-b",["main","sub"]));
        Assert(grid.Tiles[0].ActualRole=="sub"&&grid.Tiles[1].ActualRole=="sub");
        await grid.EnterFocusAsync(0);
        Assert(grid.FocusedTile==0&&grid.Tiles[0].ActualRole=="main");
        Assert(grid.Tiles[1].HasAssignment&&grid.Tiles[1].State==LiveTileState.Empty);
        await grid.ExitFocusAsync();
        Assert(grid.FocusedTile is null&&grid.Tiles[0].ActualRole=="sub"&&grid.Tiles[1].ActualRole=="sub");
        Assert(grid.ActiveTileCount==2);
    }

    private static async Task TestFailureIsolationAsync()
    {
        var provider=new GridProvider{FailCamera="bad"};var renderers=Renderers();
        await using var grid=Grid(provider,renderers);
        await grid.SetLayoutAsync(4);
        await grid.AssignCameraAsync(0,Camera("good",["main","sub"]));
        await grid.AssignCameraAsync(1,Camera("bad",["main","sub"]));
        Assert(grid.Tiles[0].State==LiveTileState.Live);
        Assert(grid.Tiles[1].State==LiveTileState.Failed);
        Assert(grid.ActiveTileCount==1&&grid.FailedTileCount==1);
    }

    private static async Task TestRapidReplacementFencingAsync()
    {
        var provider=new DelayedProvider();var renderers=Renderers();
        await using var grid=Grid(provider,renderers);
        var first=grid.AssignCameraAsync(0,Camera("slow-a",["main","sub"]));
        await provider.FirstStarted.Task.WaitAsync(TimeSpan.FromSeconds(2));
        var second=grid.AssignCameraAsync(0,Camera("fast-b",["main","sub"]));
        provider.ReleaseFirst.TrySetResult();
        await Task.WhenAll(first,second);
        Assert(grid.Tiles[0].CameraId=="fast-b");
        Assert(grid.Tiles[0].ActualRole=="main");
        Assert(grid.Tiles[0].State==LiveTileState.Live);
    }

    private static async Task TestPersistenceAndAuthorizationRestoreAsync()
    {
        var dir=Path.Combine(Path.GetTempPath(),"ivms-live-grid-tests",Guid.NewGuid().ToString("N"));
        var path=Path.Combine(dir,"live-grid.json");var profile=Guid.NewGuid();var store=new LiveGridSettingsStore(path);
        var snapshot=new LiveGridSnapshot(4,2,["authorized","removed",null,null]);
        await store.SaveAsync(profile,snapshot);
        var raw=await File.ReadAllTextAsync(path);
        Assert(!raw.Contains("token",StringComparison.OrdinalIgnoreCase));
        Assert(!raw.Contains("webrtc",StringComparison.OrdinalIgnoreCase));
        Assert(!raw.Contains("password",StringComparison.OrdinalIgnoreCase));
        var loaded=await store.LoadAsync(profile);Assert(loaded is not null&&loaded.LayoutCount==4&&loaded.SelectedTile==2);

        var provider=new GridProvider();var renderers=Renderers();
        await using var grid=Grid(provider,renderers);
        var authorized=new Dictionary<string,CameraInfo>(StringComparer.Ordinal)
        {
            ["authorized"]=Camera("authorized",["main","sub"])
        };
        await grid.RestoreAssignmentsAsync(loaded,authorized);
        Assert(grid.Tiles[0].CameraId=="authorized"&&grid.Tiles[0].State==LiveTileState.Empty);
        Assert(!grid.Tiles[1].HasAssignment&&grid.ActiveTileCount==0);
    }

    private static async Task TestStopAllAndRepeatedLifecycleAsync()
    {
        var provider=new GridProvider();var renderers=Renderers();
        await using var grid=Grid(provider,renderers);
        await grid.SetLayoutAsync(4);
        for(var iteration=0;iteration<20;iteration++)
        {
            var index=iteration%4;
            await grid.AssignCameraAsync(index,Camera($"loop-{iteration}",["main","sub"]));
            await grid.ClearTileAsync(index);
        }
        await grid.AssignCameraAsync(0,Camera("final-a",["main","sub"]));
        await grid.AssignCameraAsync(1,Camera("final-b",["main","sub"]));
        await grid.StopAllAsync();
        Assert(grid.ActiveTileCount==0&&grid.ConnectingTileCount==0);
        Assert(grid.Tiles[0].HasAssignment&&grid.Tiles[1].HasAssignment);
        Assert(renderers.Sum(x=>x.Stops)>=22);
    }

    private static LiveGridCoordinator Grid(ILiveAccessProvider provider,IReadOnlyList<GridRenderer> renderers)
    {
        var grid=new LiveGridCoordinator(provider,new ServerProfile(Guid.NewGuid(),"Test","https","vms.example.test",443),new GridLogger(),4);
        for(var i=0;i<renderers.Count;i++)grid.RegisterTile(i,renderers[i]);
        return grid;
    }

    private static List<GridRenderer> Renderers()=>Enumerable.Range(0,16).Select(_=>new GridRenderer()).ToList();

    private static CameraInfo Camera(string id,string[] roles)=>new()
    {
        Id=id,TenantId="tenant-a",SiteId="site-a",Name=id,Enabled=true,DesiredState="provisioned",AvailableLiveRoles=roles
    };

    private static void Assert(bool condition){if(!condition)throw new InvalidOperationException("Live-grid assertion failed.");}
    private static void Throws<T>(Action action) where T:Exception{try{action();}catch(T){return;}throw new InvalidOperationException($"Expected {typeof(T).Name}.");}
    private static async Task ThrowsAsync<T>(Func<Task> action) where T:Exception{try{await action();}catch(T){return;}throw new InvalidOperationException($"Expected {typeof(T).Name}.");}

    private sealed class GridProvider:ILiveAccessProvider
    {
        public string? FailCamera{get;init;}
        public virtual Task<LiveAccessGrant> GetLiveAccessAsync(string cameraId,string role,CancellationToken cancellationToken=default)
        {
            if(cameraId==FailCamera)throw new VmsApiException(HttpStatusCode.ServiceUnavailable);
            return Task.FromResult(Grant(cameraId,role));
        }
        protected static LiveAccessGrant Grant(string cameraId,string role)=>new()
        {
            CameraId=cameraId,StreamRole=role,Path=$"safe-{cameraId}-{role}",
            WebRtcUrl=$"https://media.example.test/{cameraId}/{role}",AccessToken="short-lived-test-grant",
            ExpiresAt=DateTimeOffset.UtcNow.AddMinutes(1)
        };
    }

    private sealed class DelayedProvider:GridProvider
    {
        public TaskCompletionSource FirstStarted{get;}=new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource ReleaseFirst{get;}=new(TaskCreationOptions.RunContinuationsAsynchronously);
        public override async Task<LiveAccessGrant> GetLiveAccessAsync(string cameraId,string role,CancellationToken cancellationToken=default)
        {
            if(cameraId=="slow-a")
            {
                FirstStarted.TrySetResult();
                await ReleaseFirst.Task.WaitAsync(cancellationToken);
            }
            return Grant(cameraId,role);
        }
    }

    private sealed class GridRenderer:ILiveMediaRenderer
    {
        public int Starts{get;private set;} public int Stops{get;private set;}
        public string State{get;private set;}="IDLE";
        public event EventHandler<LiveRendererStateChangedEventArgs>? StateChanged;
        public Task StartAsync(LiveAccessGrant grant,CancellationToken cancellationToken=default)
        {
            Starts++;State="LIVE";StateChanged?.Invoke(this,new LiveRendererStateChangedEventArgs(State));return Task.CompletedTask;
        }
        public Task StopAsync(CancellationToken cancellationToken=default)
        {
            Stops++;State="IDLE";StateChanged?.Invoke(this,new LiveRendererStateChangedEventArgs(State));return Task.CompletedTask;
        }
    }

    private sealed class GridLogger:IClientLogger
    {
        public void Info(string subsystem,string message){}
        public void Warning(string subsystem,string message){}
        public void LogError(string subsystem,string message,Exception? exception=null){}
    }
}
