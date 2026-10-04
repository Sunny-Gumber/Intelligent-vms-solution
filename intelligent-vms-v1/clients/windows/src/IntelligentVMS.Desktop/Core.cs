using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;

namespace IntelligentVMS.Desktop;

public static class ClientPaths
{
    public static string Root => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "IntelligentVMS", "Client");
    public static string ConfigDirectory => Path.Combine(Root, "config");
    public static string LogDirectory => Path.Combine(Root, "logs");
    public static string CacheDirectory => Path.Combine(Root, "cache");
    public static string WebView2CacheDirectory => Path.Combine(CacheDirectory, "webview2");
    public static string ProfilesFile => Path.Combine(ConfigDirectory, "profiles.json");
    public static string LiveGridFile(Guid profileId) => Path.Combine(ConfigDirectory, $"live-grid-{profileId:D}.json");
    public static void EnsureCreated()
    {
        Directory.CreateDirectory(ConfigDirectory);
        Directory.CreateDirectory(LogDirectory);
        Directory.CreateDirectory(CacheDirectory);
        Directory.CreateDirectory(WebView2CacheDirectory);
    }
}

public sealed record ServerProfile(Guid Id, string DisplayName, string Scheme, string Host, int Port)
{
    public Uri ApiBaseAddress => new UriBuilder(Scheme, Host, Port).Uri;
    public string SafeAddress => $"{Scheme}://{Host}:{Port}";
}
public sealed record ValidationResult(bool IsValid, string Message)
{
    public static ValidationResult Ok() => new(true, "");
    public static ValidationResult Fail(string message) => new(false, message);
}
public enum DesktopSessionState { SignedOut, Authenticating, Authenticated, Expired }
public enum AuthenticationUxState { Disconnected, Connecting, AuthenticationRequired, Authenticating, Authenticated, Refreshing, Expired, SigningOut, OfflineWithRestorableSession, Failed }
public enum ServerConnectionState { Disconnected, Connecting, Connected, AuthenticationRequired, Unreachable, TlsError, Incompatible }

public sealed class SessionInfo
{
    [JsonPropertyName("authenticated")] public bool Authenticated { get; set; }
    [JsonPropertyName("roles")] public string[] Roles { get; set; } = [];
    [JsonPropertyName("tenant_id")] public string TenantId { get; set; } = "";
    [JsonPropertyName("site_ids")] public string[] SiteIds { get; set; } = [];
}
public sealed class AuthenticationCapabilities
{
    [JsonPropertyName("authentication_required")] public bool AuthenticationRequired { get; set; }
    [JsonPropertyName("manual_token_login")] public bool ManualTokenLogin { get; set; }
    [JsonPropertyName("remember_session")] public bool RememberSession { get; set; }
    [JsonPropertyName("oidc")] public OidcCapability Oidc { get; set; } = new();
}
public sealed class OidcCapability
{
    [JsonPropertyName("enabled")] public bool Enabled { get; set; }
    [JsonPropertyName("required")] public bool Required { get; set; }
    [JsonPropertyName("authority")] public string Authority { get; set; } = "";
    [JsonPropertyName("client_id")] public string ClientId { get; set; } = "";
    [JsonPropertyName("scopes")] public string[] Scopes { get; set; } = [];
    [JsonPropertyName("callback")] public string Callback { get; set; } = "";
    [JsonPropertyName("pkce_methods")] public string[] PkceMethods { get; set; } = [];
}
public sealed class ServerCapabilities
{
    [JsonPropertyName("deployment_profile")] public string DeploymentProfile { get; set; } = "";
    [JsonPropertyName("event_pipeline")] public bool EventPipeline { get; set; }
    [JsonPropertyName("event_history")] public bool EventHistory { get; set; }
    [JsonPropertyName("alarm_processing")] public bool AlarmProcessing { get; set; }
    [JsonPropertyName("ai_ui")] public bool AiUi { get; set; }
    [JsonPropertyName("distributed_placement")] public bool DistributedPlacement { get; set; }
}
public sealed class CameraInfo
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("tenant_id")] public string TenantId { get; set; } = "";
    [JsonPropertyName("site_id")] public string SiteId { get; set; } = "";
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("enabled")] public bool Enabled { get; set; }
    [JsonPropertyName("desired_state")] public string DesiredState { get; set; } = "";
    [JsonPropertyName("available_live_roles")] public string[] AvailableLiveRoles { get; set; } = ["main"];
}
public sealed class RecordingSpanDto
{
    [JsonPropertyName("start")] public DateTimeOffset Start { get; set; }
    [JsonPropertyName("duration")] public double Duration { get; set; }
    [JsonPropertyName("end")] public DateTimeOffset End { get; set; }
}

public sealed class LiveAccessGrant
{
    [JsonPropertyName("camera_id")] public string CameraId { get; set; } = "";
    [JsonPropertyName("stream_role")] public string StreamRole { get; set; } = "";
    [JsonPropertyName("path")] public string Path { get; set; } = "";
    [JsonPropertyName("webrtc_url")] public string WebRtcUrl { get; set; } = "";
    [JsonPropertyName("access_token")] public string AccessToken { get; set; } = "";
    [JsonPropertyName("expires_at")] public DateTimeOffset ExpiresAt { get; set; }
}
public sealed record CameraSiteGroup(string SiteId, IReadOnlyList<CameraInfo> Cameras);
public sealed record ClientDiagnostics(string ApplicationVersion, string OsVersion, string Architecture, string ServerAddress,
    string ConnectionState, string DeploymentProfile, string LogLocation, string MediaState,
    string AuthenticationMode = "none", string AuthenticationState = "SignedOut", bool RememberedSession = false,
    string TokenExpiry = "unknown", string OidcIssuerHost = "not configured", string LastAuthErrorCategory = "none",
    string CallbackMechanism = "none", string CredentialStoreStatus = "not used",
    string LiveLayout = "1-view", int ActiveTiles = 0, int ConnectingTiles = 0, int FailedTiles = 0,
    string LiveTileStates = "none", string RendererType = "WebView2-WHEP");

public static class ServerProfileIdentity
{
    public static bool SameCredentialOrigin(ServerProfile left, ServerProfile right) =>
        string.Equals(left.Scheme, right.Scheme, StringComparison.OrdinalIgnoreCase) &&
        string.Equals(left.Host, right.Host, StringComparison.OrdinalIgnoreCase) &&
        left.Port == right.Port;
}

public static class ServerProfileValidator
{
    public static ValidationResult Validate(ServerProfile profile)
    {
        if (profile.Id == Guid.Empty) return ValidationResult.Fail("Server profile ID is invalid.");
        if (string.IsNullOrWhiteSpace(profile.DisplayName) || profile.DisplayName.Length > 128) return ValidationResult.Fail("Enter a server display name.");
        if (profile.Scheme is not ("http" or "https")) return ValidationResult.Fail("Server scheme must be HTTP or HTTPS.");
        if (profile.Port is < 1 or > 65535) return ValidationResult.Fail("Server port is invalid.");
        if (string.IsNullOrWhiteSpace(profile.Host) || profile.Host.Length > 253 || profile.Host.Any(char.IsWhiteSpace) ||
            profile.Host.Contains('/') || profile.Host.Contains('@')) return ValidationResult.Fail("Server hostname or IP is invalid.");
        if (Uri.CheckHostName(profile.Host) == UriHostNameType.Unknown && !profile.Host.Equals("localhost", StringComparison.OrdinalIgnoreCase))
            return ValidationResult.Fail("Server hostname or IP is invalid.");
        if (profile.Scheme == "http" && !IsLoopback(profile.Host))
            return ValidationResult.Fail("Remote VMS servers require HTTPS. HTTP is allowed only for localhost field testing.");
        return ValidationResult.Ok();
    }
    public static bool IsLoopback(string host) =>
        host.Equals("localhost", StringComparison.OrdinalIgnoreCase) ||
        (IPAddress.TryParse(host, out var address) && IPAddress.IsLoopback(address));
}

public sealed class JsonProfileStore
{
    private readonly string _path;
    private static readonly JsonSerializerOptions JsonOptions = new() { WriteIndented = true };
    public JsonProfileStore(string? path = null) => _path = path ?? ClientPaths.ProfilesFile;

    public async Task<IReadOnlyList<ServerProfile>> LoadAsync(CancellationToken cancellationToken = default)
    {
        if (!File.Exists(_path)) return [];
        try
        {
            await using var stream = File.OpenRead(_path);
            return await JsonSerializer.DeserializeAsync<List<ServerProfile>>(stream, JsonOptions, cancellationToken) ?? [];
        }
        catch (JsonException)
        {
            var quarantine = _path + ".corrupt";
            if (File.Exists(quarantine)) File.Delete(quarantine);
            File.Move(_path, quarantine);
            return [];
        }
    }
    public async Task SaveAsync(IReadOnlyCollection<ServerProfile> profiles, CancellationToken cancellationToken = default)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(_path)!);
        var temporary = _path + ".tmp";
        await using (var stream = File.Create(temporary))
            await JsonSerializer.SerializeAsync(stream, profiles, JsonOptions, cancellationToken);
        File.Move(temporary, _path, true);
    }
}

public static class CameraTreeBuilder
{
    public static IReadOnlyList<CameraSiteGroup> Build(IEnumerable<CameraInfo> cameras) =>
        cameras.GroupBy(c => c.SiteId, StringComparer.OrdinalIgnoreCase).OrderBy(g => g.Key, StringComparer.OrdinalIgnoreCase)
            .Select(g => new CameraSiteGroup(g.Key, g.OrderBy(c => c.Name, StringComparer.OrdinalIgnoreCase).ToArray())).ToArray();
}

public static partial class SecretRedactor
{
    [GeneratedRegex(@"(?i)Bearer\s+[A-Za-z0-9._~+\-/]+=*")] private static partial Regex BearerRegex();
    [GeneratedRegex(@"(?i)\b((?:rtsp|rtsps|http|https)://)[^/\s:@]+:[^@\s/]+@")] private static partial Regex CredentialUriRegex();
    [GeneratedRegex(@"(?i)(authorization|access_token|refresh_token|id_token|password|token|code|code_verifier)=([^\s&]+)")] private static partial Regex NamedSecretRegex();
    public static string Redact(string? value)
    {
        var text = BearerRegex().Replace(value ?? string.Empty, "Bearer [REDACTED]");
        text = CredentialUriRegex().Replace(text, "$1[REDACTED]@");
        return NamedSecretRegex().Replace(text, "$1=[REDACTED]");
    }
}

public interface IClientLogger
{
    void Info(string subsystem, string message);
    void Warning(string subsystem, string message);
    void LogError(string subsystem, string message, Exception? exception = null);
}
public sealed class BoundedFileLogger : IClientLogger
{
    private readonly object _gate = new();
    private readonly string _directory;
    private const long MaxBytes = 2 * 1024 * 1024;
    private const int MaxFiles = 5;
    public BoundedFileLogger(string directory) { _directory = directory; Directory.CreateDirectory(directory); }
    public void Info(string subsystem, string message) => Write("INFO", subsystem, message, null);
    public void Warning(string subsystem, string message) => Write("WARN", subsystem, message, null);
    public void LogError(string subsystem, string message, Exception? exception = null) => Write("ERROR", subsystem, message, exception);
    private void Write(string level, string subsystem, string message, Exception? exception)
    {
        lock (_gate)
        {
            RotateIfNeeded();
            var safe = SecretRedactor.Redact(message);
            var detail = exception is null ? "" : $" error_type={exception.GetType().Name}";
            File.AppendAllText(Path.Combine(_directory, "desktop.log"),
                $"{DateTimeOffset.UtcNow:O} level={level} subsystem={SafeToken(subsystem)} message={safe}{detail}{Environment.NewLine}");
        }
    }
    private void RotateIfNeeded()
    {
        var current = Path.Combine(_directory, "desktop.log");
        var file = new FileInfo(current);
        if (!file.Exists || file.Length < MaxBytes) return;
        for (var i = MaxFiles - 1; i >= 1; i--)
        {
            var source = Path.Combine(_directory, i == 1 ? "desktop.log" : $"desktop.{i - 1}.log");
            var destination = Path.Combine(_directory, $"desktop.{i}.log");
            if (File.Exists(destination)) File.Delete(destination);
            if (File.Exists(source)) File.Move(source, destination);
        }
    }
    private static string SafeToken(string value) => new(value.Where(c => char.IsLetterOrDigit(c) || c is '-' or '_' or '.').Take(48).ToArray());
}

public interface ICredentialStore
{
    Task SaveAsync(Guid profileId, string token, CancellationToken cancellationToken = default);
    Task<string?> LoadAsync(Guid profileId, CancellationToken cancellationToken = default);
    Task DeleteAsync(Guid profileId, CancellationToken cancellationToken = default);
}
public sealed class WindowsCredentialStore : ICredentialStore
{
    private const int CredTypeGeneric = 1, CredPersistLocalMachine = 2;
    private const int MaxCredentialBlobBytes = 5 * 512;
    private readonly string _purpose;
    public WindowsCredentialStore(string purpose = "session")
    {
        if (string.IsNullOrWhiteSpace(purpose) || purpose.Any(ch => !(char.IsLetterOrDigit(ch) || ch is '-' or '_')))
            throw new ArgumentException("Credential purpose is invalid.", nameof(purpose));
        _purpose = purpose;
    }
    private string Target(Guid id) => _purpose == "session"
        ? $"IntelligentVMS.Desktop/{id:D}"
        : $"IntelligentVMS.Desktop/{id:D}/{_purpose}";
    public Task SaveAsync(Guid profileId, string token, CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (!OperatingSystem.IsWindows()) throw new PlatformNotSupportedException();
        if (string.IsNullOrWhiteSpace(token)) throw new ArgumentException("Token is required.", nameof(token));
        var bytes = Encoding.Unicode.GetBytes(token);
        if(bytes.Length > MaxCredentialBlobBytes){Array.Clear(bytes);throw new InvalidOperationException("Protected credential exceeds Windows Credential Manager capacity.");}
        var blob = Marshal.AllocCoTaskMem(bytes.Length + 2);
        try
        {
            Marshal.Copy(bytes, 0, blob, bytes.Length);
            Marshal.WriteInt16(blob, bytes.Length, 0);
            var credential = new NativeCredential { Type = CredTypeGeneric, TargetName = Target(profileId), CredentialBlobSize = bytes.Length,
                CredentialBlob = blob, Persist = CredPersistLocalMachine, UserName = "vms-session" };
            if (!CredWrite(ref credential, 0)) throw new InvalidOperationException($"Credential Manager write failed ({Marshal.GetLastWin32Error()}).");
        }
        finally { Array.Clear(bytes); Marshal.ZeroFreeCoTaskMemUnicode(blob); }
        return Task.CompletedTask;
    }
    public Task<string?> LoadAsync(Guid profileId, CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (!OperatingSystem.IsWindows()) throw new PlatformNotSupportedException();
        if (!CredRead(Target(profileId), CredTypeGeneric, 0, out var ptr)) return Task.FromResult<string?>(null);
        try
        {
            var credential = Marshal.PtrToStructure<NativeCredential>(ptr);
            return Task.FromResult<string?>(credential.CredentialBlob == IntPtr.Zero || credential.CredentialBlobSize <= 0
                ? null : Marshal.PtrToStringUni(credential.CredentialBlob, credential.CredentialBlobSize / 2));
        }
        finally { CredFree(ptr); }
    }
    public Task DeleteAsync(Guid profileId, CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (!OperatingSystem.IsWindows()) throw new PlatformNotSupportedException();
        if (!CredDelete(Target(profileId), CredTypeGeneric, 0) && Marshal.GetLastWin32Error() != 1168)
            throw new InvalidOperationException($"Credential Manager delete failed ({Marshal.GetLastWin32Error()}).");
        return Task.CompletedTask;
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct NativeCredential
    {
        public int Flags, Type; [MarshalAs(UnmanagedType.LPWStr)] public string? TargetName; [MarshalAs(UnmanagedType.LPWStr)] public string? Comment;
        public long LastWritten; public int CredentialBlobSize; public IntPtr CredentialBlob; public int Persist, AttributeCount; public IntPtr Attributes;
        [MarshalAs(UnmanagedType.LPWStr)] public string? TargetAlias; [MarshalAs(UnmanagedType.LPWStr)] public string? UserName;
    }
    [DllImport("advapi32.dll", EntryPoint="CredWriteW", CharSet=CharSet.Unicode, SetLastError=true)][return:MarshalAs(UnmanagedType.Bool)]
    private static extern bool CredWrite(ref NativeCredential credential, int flags);
    [DllImport("advapi32.dll", EntryPoint="CredReadW", CharSet=CharSet.Unicode, SetLastError=true)][return:MarshalAs(UnmanagedType.Bool)]
    private static extern bool CredRead(string target, int type, int reservedFlag, out IntPtr credentialPtr);
    [DllImport("advapi32.dll", EntryPoint="CredDeleteW", CharSet=CharSet.Unicode, SetLastError=true)][return:MarshalAs(UnmanagedType.Bool)]
    private static extern bool CredDelete(string target, int type, int flags);
    [DllImport("advapi32.dll")] private static extern void CredFree(IntPtr buffer);
}

public sealed class DesktopSession
{
    private readonly ICredentialStore _credentials; private readonly IClientLogger _logger;
    public DesktopSession(ICredentialStore credentials, IClientLogger logger) { _credentials=credentials; _logger=logger; }
    public DesktopSessionState State { get; private set; } = DesktopSessionState.SignedOut;
    public string? AccessToken { get; private set; }
    public Guid? ProfileId { get; private set; }
    public bool RememberedSession { get; private set; }
    public async Task<SessionInfo> AuthenticateAsync(Guid profileId, string token, bool remember,
        Func<CancellationToken,Task<SessionInfo>> validate, CancellationToken cancellationToken=default)
    {
        if (string.IsNullOrWhiteSpace(token)) throw new ArgumentException("Access token is required.", nameof(token));
        ProfileId=profileId; AccessToken=token.Trim(); State=DesktopSessionState.Authenticating;
        try
        {
            var info=await validate(cancellationToken);
            if(!info.Authenticated) throw new UnauthorizedAccessException();
            if(remember) await _credentials.SaveAsync(profileId,AccessToken,cancellationToken); else await _credentials.DeleteAsync(profileId,cancellationToken);
            RememberedSession=remember; State=DesktopSessionState.Authenticated; _logger.Info("auth","desktop session authenticated"); return info;
        }
        catch { AccessToken=null; ProfileId=null; RememberedSession=false; State=DesktopSessionState.SignedOut; throw; }
    }
    public async Task<bool> TryRestoreAsync(Guid profileId, Func<CancellationToken,Task<SessionInfo>> validate, CancellationToken cancellationToken=default)
    {
        var token=await _credentials.LoadAsync(profileId,cancellationToken); if(string.IsNullOrWhiteSpace(token)) return false;
        try { await AuthenticateAsync(profileId,token,true,validate,cancellationToken); return true; }
        catch (SessionExpiredException) { await _credentials.DeleteAsync(profileId,cancellationToken); return false; }
        catch (UnauthorizedAccessException) { await _credentials.DeleteAsync(profileId,cancellationToken); return false; }
        catch { AccessToken=null; ProfileId=null; RememberedSession=false; State=DesktopSessionState.SignedOut; _logger.Warning("auth","remembered session restore unavailable"); return false; }
    }
    public Task ForgetRememberedAsync(Guid profileId, CancellationToken cancellationToken=default) =>
        _credentials.DeleteAsync(profileId, cancellationToken);

    public async Task LogoutAsync(CancellationToken cancellationToken=default)
    {
        if(ProfileId is Guid id) await _credentials.DeleteAsync(id,cancellationToken);
        AccessToken=null; ProfileId=null; RememberedSession=false; State=DesktopSessionState.SignedOut; _logger.Info("auth","desktop session signed out");
    }
    public void ReplaceAccessToken(Guid profileId, string token)
    {
        if(State!=DesktopSessionState.Authenticated || ProfileId!=profileId || string.IsNullOrWhiteSpace(token))
            throw new InvalidOperationException("Cannot replace token outside the active authenticated profile.");
        AccessToken=token.Trim();
    }
    public void DisconnectRuntime()
    {
        AccessToken=null; ProfileId=null; RememberedSession=false; State=DesktopSessionState.SignedOut;
    }
    public async Task MarkExpiredAsync(CancellationToken cancellationToken=default)
    {
        if(ProfileId is Guid id) await _credentials.DeleteAsync(id,cancellationToken);
        AccessToken=null; ProfileId=null; RememberedSession=false; State=DesktopSessionState.Expired; _logger.Warning("auth","desktop session expired");
    }
}

public sealed class VmsApiException : Exception
{
    public HttpStatusCode StatusCode { get; }
    public VmsApiException(HttpStatusCode statusCode) : base($"VMS request failed ({(int)statusCode}).") => StatusCode=statusCode;
}
public sealed class SessionExpiredException : Exception { public SessionExpiredException() : base("The VMS session has expired."){} }
public sealed class IncompatibleServerException : Exception { public IncompatibleServerException() : base("The VMS server is incompatible with this client."){} }

public interface ILiveAccessProvider
{
    Task<LiveAccessGrant> GetLiveAccessAsync(string cameraId,string role,CancellationToken cancellationToken=default);
}
public sealed class VmsApiClient : ILiveAccessProvider, IPlaybackProvider, IDisposable
{
    private readonly HttpClient _http; private readonly Func<string?> _tokenProvider;
    private readonly Func<CancellationToken,Task<bool>>? _refreshProvider;
    private readonly JsonSerializerOptions _json=new(){PropertyNameCaseInsensitive=true}; private ServerProfile? _profile;
    public VmsApiClient(Func<string?> tokenProvider,HttpMessageHandler? handler=null,Func<CancellationToken,Task<bool>>? refreshProvider=null)
    {
        _tokenProvider=tokenProvider; _refreshProvider=refreshProvider;
        handler ??= new HttpClientHandler { CheckCertificateRevocationList=true };
        _http=new HttpClient(handler){Timeout=TimeSpan.FromSeconds(12)};
    }
    public void Configure(ServerProfile profile)
    {
        var v=ServerProfileValidator.Validate(profile); if(!v.IsValid) throw new ArgumentException(v.Message,nameof(profile)); _profile=profile;
    }
    public async Task<ServerConnectionState> TestConnectionAsync(CancellationToken cancellationToken=default)
    {
        try
        {
            using var response=await _http.SendAsync(new HttpRequestMessage(HttpMethod.Get,UriFor("/api/v1/system/health")),HttpCompletionOption.ResponseHeadersRead,cancellationToken);
            if(response.StatusCode==HttpStatusCode.Unauthorized) return ServerConnectionState.AuthenticationRequired;
            return response.IsSuccessStatusCode?ServerConnectionState.Connected:ServerConnectionState.Unreachable;
        }
        catch(HttpRequestException ex) when(ex.InnerException is System.Security.Authentication.AuthenticationException){return ServerConnectionState.TlsError;}
        catch(HttpRequestException){return ServerConnectionState.Unreachable;}
        catch(TaskCanceledException) when(!cancellationToken.IsCancellationRequested){return ServerConnectionState.Unreachable;}
    }
    public Task<AuthenticationCapabilities> GetAuthenticationCapabilitiesAsync(CancellationToken ct=default)=>
        SendJsonAsync<AuthenticationCapabilities>(HttpMethod.Get,"/api/v1/auth/capabilities",false,false,ct);
    public Task<SessionInfo> GetSessionAsync(CancellationToken ct=default)=>SendJsonAsync<SessionInfo>(HttpMethod.Get,"/api/v1/auth/session",true,true,ct);
    public Task<SessionInfo> GetSessionWithoutRefreshAsync(CancellationToken ct=default)=>SendJsonAsync<SessionInfo>(HttpMethod.Get,"/api/v1/auth/session",true,false,ct);
    public async Task<ServerCapabilities> GetCapabilitiesAsync(CancellationToken ct=default)
    {
        var v=await SendJsonAsync<ServerCapabilities>(HttpMethod.Get,"/api/v1/system/capabilities",true,true,ct);
        if(string.IsNullOrWhiteSpace(v.DeploymentProfile)) throw new IncompatibleServerException(); return v;
    }
    public Task<List<CameraInfo>> GetCamerasAsync(CancellationToken ct=default)=>SendJsonAsync<List<CameraInfo>>(HttpMethod.Get,"/api/v1/cameras",true,true,ct);
    public Task<LiveAccessGrant> GetLiveAccessAsync(string cameraId,string role,CancellationToken cancellationToken=default)=>SendJsonAsync<LiveAccessGrant>(
        HttpMethod.Post,$"/api/v1/live/cameras/{Uri.EscapeDataString(cameraId)}/access?stream_role={Uri.EscapeDataString(role)}",true,true,cancellationToken);

    public Task EnsurePlaybackAuthorizedAsync(CancellationToken cancellationToken=default)=>
        GetSessionAsync(cancellationToken);

    public async Task<IReadOnlyList<RecordingSpanDto>> GetRecordingTimelineAsync(string cameraId,DateTimeOffset start,DateTimeOffset end,CancellationToken cancellationToken=default)
    {
        var startText=start.UtcDateTime.ToString("O",System.Globalization.CultureInfo.InvariantCulture);
        var endText=end.UtcDateTime.ToString("O",System.Globalization.CultureInfo.InvariantCulture);
        var relative=$"/api/v1/recordings/cameras/{Uri.EscapeDataString(cameraId)}/timeline?start={Uri.EscapeDataString(startText)}&end={Uri.EscapeDataString(endText)}";
        return await SendJsonAsync<List<RecordingSpanDto>>(HttpMethod.Get,relative,true,true,cancellationToken);
    }

    public Uri BuildPlaybackUri(string cameraId,DateTimeOffset start,double durationSeconds)
    {
        if(durationSeconds<=0||durationSeconds>14400)throw new ArgumentOutOfRangeException(nameof(durationSeconds));
        var startText=start.UtcDateTime.ToString("O",System.Globalization.CultureInfo.InvariantCulture);
        var durationText=durationSeconds.ToString(System.Globalization.CultureInfo.InvariantCulture);
        return UriFor($"/api/v1/recordings/cameras/{Uri.EscapeDataString(cameraId)}/play?start={Uri.EscapeDataString(startText)}&duration={durationText}&format=mp4");
    }
    public async Task ExportClipAsync(string cameraId,DateTimeOffset start,double durationSeconds,Stream destination,CancellationToken cancellationToken=default)
    {
        if(durationSeconds<=0)throw new ArgumentOutOfRangeException(nameof(durationSeconds));
        var startText=start.UtcDateTime.ToString("O",System.Globalization.CultureInfo.InvariantCulture);
        var durationText=durationSeconds.ToString(System.Globalization.CultureInfo.InvariantCulture);
        var relative=$"/api/v1/recordings/cameras/{Uri.EscapeDataString(cameraId)}/export?start={Uri.EscapeDataString(startText)}&duration={durationText}";
        for(var attempt=0;attempt<2;attempt++)
        {
            using var request=new HttpRequestMessage(HttpMethod.Get,UriFor(relative));
            var credential=_tokenProvider();
            if(string.IsNullOrWhiteSpace(credential))throw new SessionExpiredException();
            request.Headers.Authorization=new AuthenticationHeaderValue("Bearer",credential);
            using var response=await _http.SendAsync(request,HttpCompletionOption.ResponseHeadersRead,cancellationToken);
            if(response.StatusCode==HttpStatusCode.Unauthorized)
            {
                if(attempt==0&&_refreshProvider is not null&&await _refreshProvider(cancellationToken))continue;
                throw new SessionExpiredException();
            }
            if(!response.IsSuccessStatusCode)throw new VmsApiException(response.StatusCode);
            await response.Content.CopyToAsync(destination,cancellationToken);
            return;
        }
        throw new SessionExpiredException();
    }

    private async Task<T> SendJsonAsync<T>(HttpMethod method,string relative,bool auth,bool allowRefresh,CancellationToken ct)
    {
        for(var attempt=0;attempt<2;attempt++)
        {
            using var request=new HttpRequestMessage(method,UriFor(relative));
            if(auth)
            {
                var token=_tokenProvider(); if(string.IsNullOrWhiteSpace(token)) throw new SessionExpiredException();
                request.Headers.Authorization=new AuthenticationHeaderValue("Bearer",token);
            }
            using var response=await _http.SendAsync(request,HttpCompletionOption.ResponseHeadersRead,ct);
            if(response.StatusCode==HttpStatusCode.Unauthorized && auth)
            {
                if(attempt==0 && allowRefresh && _refreshProvider is not null && await _refreshProvider(ct)) continue;
                throw new SessionExpiredException();
            }
            if(!response.IsSuccessStatusCode) throw new VmsApiException(response.StatusCode);
            await using var stream=await response.Content.ReadAsStreamAsync(ct);
            return await JsonSerializer.DeserializeAsync<T>(stream,_json,ct) ?? throw new IncompatibleServerException();
        }
        throw new SessionExpiredException();
    }
    private Uri UriFor(string relative){if(_profile is null) throw new InvalidOperationException("No VMS server profile is configured."); return new Uri(_profile.ApiBaseAddress,relative);}
    public void Dispose()=>_http.Dispose();
}

public sealed class LiveRendererStateChangedEventArgs(string state):EventArgs
{
    public string State { get; }=state;
}
public interface ILiveMediaRenderer
{
    string State { get; }
    event EventHandler<LiveRendererStateChangedEventArgs>? StateChanged;
    Task StartAsync(LiveAccessGrant grant,CancellationToken cancellationToken=default);
    Task StopAsync(CancellationToken cancellationToken=default);
}
public static class LiveGrantValidator
{
    public static void Validate(ServerProfile profile,string cameraId,string role,LiveAccessGrant grant)
    {
        if(grant.CameraId!=cameraId||!string.Equals(grant.StreamRole,role,StringComparison.OrdinalIgnoreCase)) throw new InvalidOperationException("Live grant mismatch.");
        if(string.IsNullOrWhiteSpace(grant.AccessToken)||grant.ExpiresAt<=DateTimeOffset.UtcNow) throw new InvalidOperationException("Live grant is missing or expired.");
        if(!Uri.TryCreate(grant.WebRtcUrl,UriKind.Absolute,out var uri)||uri.Scheme is not("http" or "https")||!string.IsNullOrEmpty(uri.UserInfo)||uri.Query.Length>0||uri.Fragment.Length>0)
            throw new InvalidOperationException("Live media endpoint is unsafe.");
        if(profile.Scheme=="https"&&uri.Scheme!="https") throw new InvalidOperationException("Live media endpoint attempted a TLS downgrade.");
        if(grant.WebRtcUrl.Contains(grant.AccessToken,StringComparison.Ordinal)) throw new InvalidOperationException("Live token must never be embedded in the media URL.");
    }
}
public sealed class LiveSessionController : IAsyncDisposable
{
    private readonly ILiveAccessProvider _provider; private readonly ILiveMediaRenderer _renderer; private readonly ServerProfile _profile; private readonly IClientLogger _logger; private int _generation;
    public LiveSessionController(ILiveAccessProvider provider,ILiveMediaRenderer renderer,ServerProfile profile,IClientLogger logger){_provider=provider;_renderer=renderer;_profile=profile;_logger=logger;}
    public string? CameraId{get;private set;} public string? Role{get;private set;} public string State=>_renderer.State;
    public async Task StartAsync(string cameraId,string role,CancellationToken ct=default)
    {
        var generation=Interlocked.Increment(ref _generation); await _renderer.StopAsync(ct); CameraId=null;Role=null;
        try
        {
            var grant=await _provider.GetLiveAccessAsync(cameraId,role,ct); if(generation!=_generation)return;
            LiveGrantValidator.Validate(_profile,cameraId,role,grant); await _renderer.StartAsync(grant,ct);
            if(generation!=_generation){await _renderer.StopAsync(ct);return;} CameraId=cameraId;Role=role;_logger.Info("live",$"live session started camera_id={cameraId} role={role}");
        }
        catch{await _renderer.StopAsync(CancellationToken.None);_logger.Warning("live",$"live session failed camera_id={cameraId} role={role}");throw;}
    }
    public async Task StopAsync(CancellationToken ct=default){Interlocked.Increment(ref _generation);await _renderer.StopAsync(ct);CameraId=null;Role=null;}
    public ValueTask DisposeAsync()=>new(StopAsync());
}

public static class DiagnosticsService
{
    private static readonly JsonSerializerOptions JsonOptions = new() { WriteIndented = true };
    public static ClientDiagnostics Build(ServerProfile? profile,ServerConnectionState state,ServerCapabilities? caps,string mediaState,
        string authenticationMode="none",string authenticationState="SignedOut",bool rememberedSession=false,DateTimeOffset? tokenExpiry=null,
        string oidcIssuerHost="not configured",string lastAuthErrorCategory="none",string callbackMechanism="none",string credentialStoreStatus="not used",
        string liveLayout="1-view",int activeTiles=0,int connectingTiles=0,int failedTiles=0,string liveTileStates="none",string rendererType="WebView2-WHEP")=>new(
        typeof(DiagnosticsService).Assembly.GetName().Version?.ToString()??"unknown",RuntimeInformation.OSDescription,RuntimeInformation.ProcessArchitecture.ToString(),
        profile?.SafeAddress??"not configured",state.ToString(),caps?.DeploymentProfile??"unknown",ClientPaths.LogDirectory,mediaState,
        authenticationMode,authenticationState,rememberedSession,tokenExpiry?.ToString("O")??"unknown",oidcIssuerHost,lastAuthErrorCategory,callbackMechanism,credentialStoreStatus,
        liveLayout,activeTiles,connectingTiles,failedTiles,liveTileStates,rendererType);
    public static async Task<string> ExportAsync(ClientDiagnostics diagnostics,string directory,CancellationToken ct=default)
    {
        Directory.CreateDirectory(directory); var path=Path.Combine(directory,$"intelligent-vms-client-diagnostics-{DateTime.UtcNow:yyyyMMddTHHmmssZ}.json");
        var json=JsonSerializer.Serialize(diagnostics,JsonOptions); await File.WriteAllTextAsync(path,SecretRedactor.Redact(json),ct); return path;
    }
}
public static class ClientSmokeVerifier
{
    public static void Verify()
    {
        ClientPaths.EnsureCreated();
        if(!Directory.Exists(ClientPaths.ConfigDirectory)) throw new InvalidOperationException("Client storage initialization failed.");
        if(!ServerProfileValidator.Validate(new(Guid.NewGuid(),"Local test","http","127.0.0.1",8000)).IsValid) throw new InvalidOperationException("Profile validation failed.");
    }
}
