using System.IO;
using System.Net;
using System.Text;
using IntelligentVMS.Desktop;

var tests=new List<(string,Func<Task>)>{
("server URL validation",TestServerProfiles),("profile persistence/corruption",TestProfiles),("credential storage",TestCredentials),
("token redaction",TestRedaction),("OIDC PKCE security contracts",OidcSecurityTests.RunAllAsync),("multi-camera live grid",LiveGridTests.RunAllAsync),("desktop PTZ foundation",PtzTests.RunAllAsync),("desktop playback foundation",PlaybackTests.RunAllAsync),("authentication transitions",TestSession),("remembered-session failure policy",TestRememberedSessionFailures),("401 expiry",TestUnauthorized),
("capability/camera parsing",TestApiParsing),("playback API refresh contract",TestPlaybackApiRefresh),("camera tree",TestCameraTree),("live API auth contract",TestLiveApiContract),("live grant/session cleanup",TestLive),("WHEP renderer contract",TestRendererContract),
("TLS/media policy",TestTls),("diagnostics",TestDiagnostics),("package isolation",TestPackaging),("interpolation guard",TestInterpolationGuard)};
var failures=0;
foreach(var (name,test) in tests){try{await test();Console.WriteLine($"PASS {name}");}catch(Exception ex){failures++;Console.Error.WriteLine($"FAIL {name}: {ex.GetType().Name}: {ex.Message}");}}
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
static async Task TestRememberedSessionFailures(){
 var credentials=new MemoryCredentials{Value="remembered"};var session=new DesktopSession(credentials,new MemoryLogger());var id=Guid.NewGuid();
 var restored=await session.TryRestoreAsync(id,_=>Task.FromException<SessionInfo>(new HttpRequestException("offline")));
 Assert(!restored&&credentials.Value=="remembered"&&session.State==DesktopSessionState.SignedOut&&session.ProfileId is null);
 credentials.Value="expired";
 restored=await session.TryRestoreAsync(id,_=>Task.FromException<SessionInfo>(new SessionExpiredException()));
 Assert(!restored&&credentials.Value is null&&session.State==DesktopSessionState.SignedOut&&session.ProfileId is null);
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
static async Task TestPlaybackApiRefresh(){
 var profile=new ServerProfile(Guid.NewGuid(),"Remote","https","vms.example.com",443);
 var token="expired";var refreshes=0;var timelineCalls=0;var timelineAuth=new List<string?>();
 using(var api=new VmsApiClient(()=>token,new StubHandler(request=>{
  timelineCalls++;timelineAuth.Add(request.Headers.Authorization?.Parameter);
  return timelineCalls==1?new HttpResponseMessage(HttpStatusCode.Unauthorized):Json(HttpStatusCode.OK,"[]");
 }),refreshProvider:_=>{refreshes++;token="renewed";return Task.FromResult(true);})){
  api.Configure(profile);
  var spans=await api.GetRecordingTimelineAsync("camera 1",DateTimeOffset.Parse("2026-10-04T00:00:00Z"),DateTimeOffset.Parse("2026-10-05T00:00:00Z"));
  Assert(spans.Count==0&&timelineCalls==2&&refreshes==1&&timelineAuth.SequenceEqual(new[]{"expired","renewed"}));
 }

 token="expired";refreshes=0;var exportCalls=0;var exportAuth=new List<string?>();
 using(var api=new VmsApiClient(()=>token,new StubHandler(request=>{
  exportCalls++;exportAuth.Add(request.Headers.Authorization?.Parameter);
  return exportCalls==1?new HttpResponseMessage(HttpStatusCode.Unauthorized):new HttpResponseMessage(HttpStatusCode.OK){Content=new ByteArrayContent(new byte[]{1,2,3})};
 }),refreshProvider:_=>{refreshes++;token="renewed";return Task.FromResult(true);})){
  api.Configure(profile);await using var destination=new MemoryStream();
  await api.ExportClipAsync("camera-1",DateTimeOffset.Parse("2026-10-04T10:00:00Z"),60,destination);
  Assert(destination.Length==3&&exportCalls==2&&refreshes==1&&exportAuth.SequenceEqual(new[]{"expired","renewed"}));
 }

 refreshes=0;
 using(var api=new VmsApiClient(()=>"valid",new StubHandler(_=>new HttpResponseMessage(HttpStatusCode.Forbidden)),refreshProvider:_=>{refreshes++;return Task.FromResult(true);})){
  api.Configure(profile);
  await Throws<VmsApiException>(()=>api.GetRecordingTimelineAsync("camera-1",DateTimeOffset.Parse("2026-10-04T00:00:00Z"),DateTimeOffset.Parse("2026-10-05T00:00:00Z")));
  Assert(refreshes==0);
 }
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
 var dir=TempDir();var p=new ServerProfile(Guid.NewGuid(),"Remote","https","vms.example.com",443);
 var path=await DiagnosticsService.ExportAsync(DiagnosticsService.Build(p,ServerConnectionState.Connected,new(){DeploymentProfile="windows-small-site"},"IDLE"),dir);var text=await File.ReadAllTextAsync(path);
 Assert(text.Contains("vms.example.com")&&!text.Contains("Bearer ")&&!text.Contains("password",StringComparison.OrdinalIgnoreCase));
}
static async Task TestPackaging(){
 var root=FindRepoRoot();var install=await File.ReadAllTextAsync(Path.Combine(root,"clients","windows","packaging","Install-Client.ps1"));var uninstall=await File.ReadAllTextAsync(Path.Combine(root,"clients","windows","packaging","Uninstall-Client.ps1"));var combined=install+uninstall;
 foreach(var forbidden in new[]{"ProgramData","IntelligentVMSControl","IntelligentVMSMedia","PostgreSQL","Stop-Service","Remove-Service"})Assert(!combined.Contains(forbidden,StringComparison.OrdinalIgnoreCase));
 Assert(combined.Contains("LocalApplicationData",StringComparison.OrdinalIgnoreCase));
}

static async Task TestLiveApiContract(){
 var handler=new RecordingHandler(_=>Json(HttpStatusCode.OK,"{\"camera_id\":\"camera 1\",\"stream_role\":\"sub\",\"path\":\"safe\",\"webrtc_url\":\"https://media.example/safe\",\"access_token\":\"grant\",\"expires_at\":\"2030-01-01T00:00:00Z\"}"));
 using var api=new VmsApiClient(()=>"desktop-token",handler);api.Configure(new(Guid.NewGuid(),"Remote","https","vms.example.com",443));
 var grant=await api.GetLiveAccessAsync("camera 1","sub");
 Assert(grant.CameraId=="camera 1"&&handler.Method==HttpMethod.Post);
 Assert(handler.Uri?.AbsolutePath=="/api/v1/live/cameras/camera%201/access");
 Assert(handler.Uri?.Query=="?stream_role=sub");
 Assert(handler.AuthorizationScheme=="Bearer"&&handler.AuthorizationParameter=="desktop-token");
}
static async Task TestRendererContract(){
 var root=FindRepoRoot();var html=await File.ReadAllTextAsync(Path.Combine(root,"clients","windows","src","IntelligentVMS.Desktop","Media","live.html"));
 Assert(html.Contains("+'/whep'",StringComparison.Ordinal));
 Assert(html.Contains("method:'POST'",StringComparison.Ordinal));
 Assert(html.Contains("method:'DELETE'",StringComparison.Ordinal));
 Assert(html.Contains("Authorization:'Bearer '+accessToken",StringComparison.Ordinal));
 Assert(html.Contains("candidate.origin!==whep.origin",StringComparison.Ordinal));
 Assert(!html.Contains("?token=",StringComparison.OrdinalIgnoreCase));
}
static HttpResponseMessage Json(HttpStatusCode code,string json)=>new(code){Content=new StringContent(json,Encoding.UTF8,"application/json")};
static string TempDir(){var p=Path.Combine(Path.GetTempPath(),"ivms-client-tests",Guid.NewGuid().ToString("N"));Directory.CreateDirectory(p);return p;}
static string FindRepoRoot(){var d=new DirectoryInfo(Directory.GetCurrentDirectory());while(d is not null){if(Directory.Exists(Path.Combine(d.FullName,"clients","windows")))return d.FullName;d=d.Parent;}throw new InvalidOperationException("Repository root not found.");}
static void Assert(bool value){if(!value)throw new InvalidOperationException("Assertion failed.");}
static async Task Throws<T>(Func<Task> action) where T:Exception{try{await action();}catch(T){return;}throw new InvalidOperationException($"Expected {typeof(T).Name}.");}
static void ThrowsSync<T>(Action action) where T:Exception{try{action();}catch(T){return;}throw new InvalidOperationException($"Expected {typeof(T).Name}.");}
static async Task TestInterpolationGuard(){
 var root=FindRepoRoot();var bad=string.Concat("$","{");
 foreach(var file in Directory.GetFiles(Path.Combine(root,"clients","windows","src"),"*.cs",SearchOption.AllDirectories)){
  Assert(!(await File.ReadAllTextAsync(file)).Contains(bad,StringComparison.Ordinal));
 }
}

sealed class StubHandler(Func<HttpRequestMessage,HttpResponseMessage> response):HttpMessageHandler{protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,CancellationToken cancellationToken)=>Task.FromResult(response(request));}
sealed class MemoryCredentials:ICredentialStore{public string? Value;public Task SaveAsync(Guid p,string t,CancellationToken c=default){Value=t;return Task.CompletedTask;}public Task<string?> LoadAsync(Guid p,CancellationToken c=default)=>Task.FromResult(Value);public Task DeleteAsync(Guid p,CancellationToken c=default){Value=null;return Task.CompletedTask;}}
sealed class MemoryLogger:IClientLogger{public void Info(string s,string m){}public void Warning(string s,string m){}public void LogError(string s,string m,Exception? e=null){}}
sealed class FakeLiveProvider:ILiveAccessProvider{public Task<LiveAccessGrant> GetLiveAccessAsync(string cameraId,string role,CancellationToken c=default)=>Task.FromResult(new LiveAccessGrant{CameraId=cameraId,StreamRole=role,Path="path",WebRtcUrl=$"https://media.example/{cameraId}",AccessToken="short-lived",ExpiresAt=DateTimeOffset.UtcNow.AddMinutes(1)});}
sealed class FakeRenderer:ILiveMediaRenderer{public int Starts,Stops;public string State{get;private set;}="IDLE";public event EventHandler<LiveRendererStateChangedEventArgs>? StateChanged;public Task StartAsync(LiveAccessGrant g,CancellationToken c=default){Starts++;State="LIVE";StateChanged?.Invoke(this,new LiveRendererStateChangedEventArgs(State));return Task.CompletedTask;}public Task StopAsync(CancellationToken c=default){Stops++;State="IDLE";StateChanged?.Invoke(this,new LiveRendererStateChangedEventArgs(State));return Task.CompletedTask;}}


sealed class RecordingHandler(Func<HttpRequestMessage,HttpResponseMessage> response):HttpMessageHandler{
 public HttpMethod? Method;public Uri? Uri;public string? AuthorizationScheme;public string? AuthorizationParameter;
 protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,CancellationToken cancellationToken){
  Method=request.Method;Uri=request.RequestUri;AuthorizationScheme=request.Headers.Authorization?.Scheme;AuthorizationParameter=request.Headers.Authorization?.Parameter;
  return Task.FromResult(response(request));
 }
}
