using System.Net;
using System.Text;
using IntelligentVMS.Desktop;

var tests=new List<(string,Func<Task>)>{
("server URL validation",TestServerProfiles),("profile persistence/corruption",TestProfiles),("credential storage",TestCredentials),
("token redaction",TestRedaction),("authentication transitions",TestSession),("401 expiry",TestUnauthorized),
("capability/camera parsing",TestApiParsing),("camera tree",TestCameraTree),("live grant/session cleanup",TestLive),
("TLS/media policy",TestTls),("diagnostics",TestDiagnostics),("package isolation",TestPackaging)};
var failures=0;
foreach(var (name,test) in tests){try{await test();Console.WriteLine($"PASS ${name}");}catch(Exception ex){failures++;Console.Error.WriteLine($"FAIL ${name}: ${ex.GetType().Name}: ${ex.Message}");}}
return failures==0?0:1;

static Task TestServerProfiles(){
 Assert(ServerProfileValidator.Validate(new(Guid.NewGuid(),"Local","http","127.0.0.1",8000)).IsValid);
 Assert(ServerProfileValidator.Validate(new(Guid.NewGuid(),"Remote","https","vms.example.com",443)).IsValid);
 Assert(!ServerProfileValidator.Validate(new(Guid.NewGuid(),"Remote","http","vms.example.com",8000)).IsValid);
 Assert(!ServerProfileValidator.Validate(new(Guid.NewGuid(),"Bad","https","user@host",443)).IsValid);
 Assert(!ServerProfileValidator.Validate(new(Guid.NewGuid(),"Bad","file","host",1)).IsValid);
 return Task.CompletedTask;
}
static async Task TestProfiles(){
 var dir=TempDir();var path=Path.Combine(dir,"profiles.json");var store=new JsonProfileStore(path);var row=new ServerProfile(Guid.NewGuid(),"HQ","https","vms.example.com",443);
 await store.SaveAsync([row]);var loaded=await store.LoadAsync();Assert(loaded.Count==1&&loaded[0]==row);
 await File.WriteAllTextAsync(path,"{bad-json");loaded=await store.LoadAsync();Assert(loaded.Count==0&&File.Exists(path+".corrupt"));
}
static async Task TestCredentials(){
 if(!OperatingSystem.IsWindows())return;var store=new WindowsCredentialStore();var id=Guid.NewGuid();var secret="test-token-"+Guid.NewGuid().ToString("N");
 try{await store.SaveAsync(id,secret);Assert(await store.LoadAsync(id)==secret);await store.DeleteAsync(id);Assert(await store.LoadAsync(id) is null);}finally{try{await store.DeleteAsync(id);}catch{}}
}
static Task TestRedaction(){
 var safe=SecretRedactor.Redact("Authorization=secret password=hunter2 Bearer abc.def.ghi rtsp://user:pass@camera/path token=xyz");
 Assert(!safe.Contains("hunter2")&&!safe.Contains("abc.def.ghi")&&!safe.Contains("user:pass")&&!safe.Contains("xyz"));return Task.CompletedTask;
}
static async Task TestSession(){
 var credentials=new MemoryCredentials();var session=new DesktopSession(credentials,new MemoryLogger());var id=Guid.NewGuid();
 var info=await session.AuthenticateAsync(id,"opaque-secret",true,_=>Task.FromResult(new SessionInfo{Authenticated=true,TenantId="t",SiteIds=["s"]}));
 Assert(info.Authenticated&&session.State==DesktopSessionState.Authenticated&&credentials.Value=="opaque-secret");
 await session.MarkExpiredAsync();Assert(session.State==DesktopSessionState.Expired&&session.AccessToken is null&&credentials.Value is null);
 await session.LogoutAsync();Assert(session.State==DesktopSessionState.SignedOut);
}
static async Task TestUnauthorized(){
 using var api=new VmsApiClient(()=>"token",new StubHandler(_=>new HttpResponseMessage(HttpStatusCode.Unauthorized)));api.Configure(new(Guid.NewGuid(),"Remote","https","vms.example.com",443));
 await Throws<SessionExpiredException>(()=>api.GetCapabilitiesAsync());
}
static async Task TestApiParsing(){
 var queue=new Queue<HttpResponseMessage>([
 Json(HttpStatusCode.OK,"{\"deployment_profile\":\"windows-small-site\",\"event_pipeline\":false,\"event_history\":false,\"alarm_processing\":false,\"ai_ui\":false,\"distributed_placement\":false}"),
 Json(HttpStatusCode.OK,"[{\"id\":\"c1\",\"tenant_id\":\"t\",\"site_id\":\"s1\",\"name\":\"Gate\",\"enabled\":true,\"desired_state\":\"provisioned\",\"available_live_roles\":[\"main\",\"sub\"]}]")]);
 using var api=new VmsApiClient(()=>"token",new StubHandler(_=>queue.Dequeue()));api.Configure(new(Guid.NewGuid(),"Remote","https","vms.example.com",443));
 var caps=await api.GetCapabilitiesAsync();var cameras=await api.GetCamerasAsync();Assert(caps.DeploymentProfile=="windows-small-site"&&!caps.EventHistory&&cameras.Count==1&&cameras[0].AvailableLiveRoles.Contains("sub"));
}
static Task TestCameraTree(){
 var tree=CameraTreeBuilder.Build([new(){Id="1",SiteId="b",Name="B"},new(){Id="2",SiteId="a",Name="Z"},new(){Id="3",SiteId="a",Name="A"}]);
 Assert(tree.Count==2&&tree[0].SiteId=="a"&&tree[0].Cameras[0].Name=="A");return Task.CompletedTask;
}
static async Task TestLive(){
 var profile=new ServerProfile(Guid.NewGuid(),"Remote","https","vms.example.com",443);var renderer=new FakeRenderer();var controller=new LiveSessionController(new FakeLiveProvider(),renderer,profile,new MemoryLogger());
 await controller.StartAsync("camera-1","sub");Assert(renderer.Starts==1&&controller.CameraId=="camera-1");
 await controller.StartAsync("camera-2","main");Assert(renderer.Stops>=2&&controller.CameraId=="camera-2");await controller.StopAsync();Assert(controller.CameraId is null&&renderer.State=="IDLE");
}
static Task TestTls(){
 var p=new ServerProfile(Guid.NewGuid(),"Remote","https","vms.example.com",443);
 ThrowsSync<InvalidOperationException>(()=>LiveGrantValidator.Validate(p,"c","main",new(){CameraId="c",StreamRole="main",WebRtcUrl="http://media.example/c",AccessToken="abc",ExpiresAt=DateTimeOffset.UtcNow.AddMinutes(1)}));
 ThrowsSync<InvalidOperationException>(()=>LiveGrantValidator.Validate(p,"c","main",new(){CameraId="c",StreamRole="main",WebRtcUrl="https://user:pass@media.example/c",AccessToken="abc",ExpiresAt=DateTimeOffset.UtcNow.AddMinutes(1)}));
 ThrowsSync<InvalidOperationException>(()=>LiveGrantValidator.Validate(p,"c","main",new(){CameraId="c",StreamRole="main",WebRtcUrl="https://media.example/c?token=abc",AccessToken="abc",ExpiresAt=DateTimeOffset.UtcNow.AddMinutes(1)}));
 return Task.CompletedTask;
}
static async Task TestDiagnostics(){
 var dir=TempDir();var service=new DiagnosticsService();var p=new ServerProfile(Guid.NewGuid(),"Remote","https","vms.example.com",443);
 var path=await service.ExportAsync(service.Build(p,ServerConnectionState.Connected,new(){DeploymentProfile="windows-small-site"},"IDLE"),dir);var text=await File.ReadAllTextAsync(path);
 Assert(text.Contains("vms.example.com")&&!text.Contains("Bearer ")&&!text.Contains("password",StringComparison.OrdinalIgnoreCase));
}
static async Task TestPackaging(){
 var root=FindRepoRoot();var install=await File.ReadAllTextAsync(Path.Combine(root,"clients","windows","packaging","Install-Client.ps1"));var uninstall=await File.ReadAllTextAsync(Path.Combine(root,"clients","windows","packaging","Uninstall-Client.ps1"));var combined=install+uninstall;
 foreach(var forbidden in new[]{"ProgramData","IntelligentVMSControl","IntelligentVMSMedia","PostgreSQL","Stop-Service","Remove-Service"})Assert(!combined.Contains(forbidden,StringComparison.OrdinalIgnoreCase));
 Assert(combined.Contains("LocalApplicationData",StringComparison.OrdinalIgnoreCase));
}
static HttpResponseMessage Json(HttpStatusCode code,string json)=>new(code){Content=new StringContent(json,Encoding.UTF8,"application/json")};
static string TempDir(){var p=Path.Combine(Path.GetTempPath(),"ivms-client-tests",Guid.NewGuid().ToString("N"));Directory.CreateDirectory(p);return p;}
static string FindRepoRoot(){var d=new DirectoryInfo(Directory.GetCurrentDirectory());while(d is not null){if(Directory.Exists(Path.Combine(d.FullName,"clients","windows")))return d.FullName;d=d.Parent;}throw new InvalidOperationException("Repository root not found.");}
static void Assert(bool value){if(!value)throw new InvalidOperationException("Assertion failed.");}
static async Task Throws<T>(Func<Task> action) where T:Exception{try{await action();}catch(T){return;}throw new InvalidOperationException($"Expected ${typeof(T).Name}.");}
static void ThrowsSync<T>(Action action) where T:Exception{try{action();}catch(T){return;}throw new InvalidOperationException($"Expected ${typeof(T).Name}.");}
sealed class StubHandler(Func<HttpRequestMessage,HttpResponseMessage> response):HttpMessageHandler{protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,CancellationToken cancellationToken)=>Task.FromResult(response(request));}
sealed class MemoryCredentials:ICredentialStore{public string? Value;public Task SaveAsync(Guid p,string t,CancellationToken c=default){Value=t;return Task.CompletedTask;}public Task<string?> LoadAsync(Guid p,CancellationToken c=default)=>Task.FromResult(Value);public Task DeleteAsync(Guid p,CancellationToken c=default){Value=null;return Task.CompletedTask;}}
sealed class MemoryLogger:IClientLogger{public void Info(string s,string m){}public void Warning(string s,string m){}public void Error(string s,string m,Exception? e=null){}}
sealed class FakeLiveProvider:ILiveAccessProvider{public Task<LiveAccessGrant> GetLiveAccessAsync(string cameraId,string role,CancellationToken c=default)=>Task.FromResult(new LiveAccessGrant{CameraId=cameraId,StreamRole=role,Path="path",WebRtcUrl=$"https://media.example/${cameraId}",AccessToken="short-lived",ExpiresAt=DateTimeOffset.UtcNow.AddMinutes(1)});}
sealed class FakeRenderer:ILiveMediaRenderer{public int Starts,Stops;public string State{get;private set;}="IDLE";public Task StartAsync(LiveAccessGrant g,CancellationToken c=default){Starts++;State="LIVE";return Task.CompletedTask;}public Task StopAsync(CancellationToken c=default){Stops++;State="IDLE";return Task.CompletedTask;}}
