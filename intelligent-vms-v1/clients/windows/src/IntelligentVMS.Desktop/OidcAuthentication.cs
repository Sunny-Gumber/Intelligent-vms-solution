using System.Diagnostics;
using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using Duende.IdentityModel.Client;
using Duende.IdentityModel.OidcClient;

namespace IntelligentVMS.Desktop;

public sealed class AuthenticationFlowException : Exception
{
    public string Category { get; }
    public AuthenticationFlowException(string category, string message) : base(message) => Category = category;
}

public static class OidcCapabilityValidator
{
    public static void Validate(OidcCapability capability)
    {
        if (!capability.Enabled) throw new AuthenticationFlowException("oidc_unavailable", "Organization sign-in is not available on this VMS server.");
        if (!Uri.TryCreate(capability.Authority, UriKind.Absolute, out var authority) || authority.Scheme != Uri.UriSchemeHttps ||
            string.IsNullOrWhiteSpace(authority.Host) || !string.IsNullOrEmpty(authority.UserInfo) || !string.IsNullOrEmpty(authority.Fragment))
            throw new AuthenticationFlowException("oidc_configuration", "The VMS server returned an unsafe identity-provider authority.");
        if (string.IsNullOrWhiteSpace(capability.ClientId) || capability.ClientId.Any(char.IsWhiteSpace))
            throw new AuthenticationFlowException("oidc_configuration", "The VMS server returned an invalid public client identifier.");
        if (!capability.Scopes.Contains("openid", StringComparer.Ordinal))
            throw new AuthenticationFlowException("oidc_configuration", "The VMS server OIDC scope contract is incompatible.");
        if (!string.Equals(capability.Callback, "loopback", StringComparison.Ordinal) ||
            !capability.PkceMethods.Contains("S256", StringComparer.Ordinal))
            throw new AuthenticationFlowException("oidc_configuration", "The VMS server does not support the required desktop OIDC security contract.");
    }
}

public static class OidcAuthorizationRequestValidator
{
    public static string Base64Url(byte[] bytes) => Convert.ToBase64String(bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_');
    public static string S256(string verifier) => Base64Url(SHA256.HashData(Encoding.ASCII.GetBytes(verifier)));

    public static IReadOnlyDictionary<string,string> Query(Uri uri)
    {
        var values = new Dictionary<string,string>(StringComparer.Ordinal);
        var raw = uri.Query.TrimStart('?');
        if (raw.Length == 0) return values;
        foreach (var pair in raw.Split('&', StringSplitOptions.RemoveEmptyEntries))
        {
            var parts = pair.Split('=', 2);
            var key = Uri.UnescapeDataString(parts[0].Replace("+", "%20", StringComparison.Ordinal));
            var value = parts.Length == 2 ? Uri.UnescapeDataString(parts[1].Replace("+", "%20", StringComparison.Ordinal)) : "";
            if (!values.TryAdd(key, value)) throw new AuthenticationFlowException("oidc_request", "OIDC authorization request contains duplicate parameters.");
        }
        return values;
    }

    public static void ValidatePrepared(Uri startUri, string redirectUri, string expectedState, string verifier, string nonce)
    {
        if (startUri.Scheme != Uri.UriSchemeHttps || !string.IsNullOrEmpty(startUri.UserInfo))
            throw new AuthenticationFlowException("oidc_request", "Identity-provider authorization endpoint must use HTTPS.");
        if (verifier.Length is < 43 or > 128 || verifier.Any(ch => !(char.IsLetterOrDigit(ch) || ch is '-' or '.' or '_' or '~')))
            throw new AuthenticationFlowException("pkce", "OIDC PKCE verifier is invalid.");
        var q = Query(startUri);
        Require(q, "response_type", "code");
        Require(q, "redirect_uri", redirectUri);
        Require(q, "state", expectedState);
        Require(q, "nonce", nonce);
        Require(q, "code_challenge_method", "S256");
        Require(q, "code_challenge", S256(verifier));
        if (q.ContainsKey("client_secret")) throw new AuthenticationFlowException("oidc_request", "Public desktop clients must not send a client secret.");
    }

    private static void Require(IReadOnlyDictionary<string,string> values, string key, string expected)
    {
        if (!values.TryGetValue(key, out var actual) || !CryptographicOperations.FixedTimeEquals(
            Encoding.UTF8.GetBytes(actual), Encoding.UTF8.GetBytes(expected)))
            throw new AuthenticationFlowException("oidc_request", $"OIDC authorization request failed {key} validation.");
    }
}

public sealed class OidcCallbackGate
{
    private int _completed;
    public string ValidateAndClaim(Uri callback, string expectedState)
    {
        if (Interlocked.CompareExchange(ref _completed, 1, 0) != 0)
            throw new AuthenticationFlowException("callback_replay", "Authentication callback was already completed.");
        if (!IPAddress.TryParse(callback.Host, out var address) || !IPAddress.IsLoopback(address) ||
            callback.AbsolutePath != "/oidc/callback/")
            throw new AuthenticationFlowException("callback_target", "Authentication callback target is invalid.");
        var q = OidcAuthorizationRequestValidator.Query(callback);
        if (!q.TryGetValue("state", out var state) || string.IsNullOrWhiteSpace(state))
            throw new AuthenticationFlowException("state_missing", "Authentication response is missing state.");
        if (!CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(state), Encoding.UTF8.GetBytes(expectedState)))
            throw new AuthenticationFlowException("state_mismatch", "Authentication response state did not match the active sign-in attempt.");
        var hasCode = q.TryGetValue("code", out var code) && !string.IsNullOrWhiteSpace(code);
        var hasError = q.TryGetValue("error", out var error) && !string.IsNullOrWhiteSpace(error);
        if (!hasCode && !hasError)
            throw new AuthenticationFlowException("callback_malformed", "Authentication response did not contain a code or error.");
        return callback.PathAndQuery;
    }
}

public sealed class LoopbackCallbackListener : IAsyncDisposable
{
    private readonly HttpListener _listener;
    private readonly OidcCallbackGate _gate = new();
    public string RedirectUri { get; }

    private LoopbackCallbackListener(HttpListener listener, string redirectUri)
    {
        _listener = listener;
        RedirectUri = redirectUri;
    }

    public static LoopbackCallbackListener Create()
    {
        for (var attempt = 0; attempt < 8; attempt++)
        {
            using var probe = new TcpListener(IPAddress.Loopback, 0);
            probe.Start();
            var port = ((IPEndPoint)probe.LocalEndpoint).Port;
            probe.Stop();
            var redirectUri = $"http://127.0.0.1:{port}/oidc/callback/";
            var listener = new HttpListener();
            listener.Prefixes.Add(redirectUri);
            try
            {
                listener.Start();
                return new LoopbackCallbackListener(listener, redirectUri);
            }
            catch (HttpListenerException)
            {
                listener.Close();
            }
        }
        throw new AuthenticationFlowException("callback_bind", "Unable to create a localhost authentication callback listener.");
    }

    public async Task<string> WaitAsync(string expectedState, TimeSpan timeout, CancellationToken cancellationToken)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        deadline.CancelAfter(timeout);
        try
        {
            while (true)
            {
                var context = await _listener.GetContextAsync().WaitAsync(deadline.Token);
                var url = context.Request.Url;
                if (url is null || url.AbsolutePath != "/oidc/callback/")
                {
                    await RespondAsync(context.Response, HttpStatusCode.NotFound, "This callback path is not available.");
                    continue;
                }
                try
                {
                    var data = _gate.ValidateAndClaim(url, expectedState);
                    await RespondAsync(context.Response, HttpStatusCode.OK, "Authentication completed. You can return to Intelligent VMS.");
                    Stop();
                    return data;
                }
                catch
                {
                    await RespondAsync(context.Response, HttpStatusCode.BadRequest, "Authentication could not be completed. Return to Intelligent VMS.");
                    Stop();
                    throw;
                }
            }
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            Stop();
            throw new AuthenticationFlowException("callback_timeout", "Authentication timed out.");
        }
    }

    private static async Task RespondAsync(HttpListenerResponse response, HttpStatusCode status, string message)
    {
        var bytes = Encoding.UTF8.GetBytes($"<!doctype html><html><body><p>{WebUtility.HtmlEncode(message)}</p></body></html>");
        response.StatusCode = (int)status;
        response.ContentType = "text/html; charset=utf-8";
        response.ContentLength64 = bytes.Length;
        await response.OutputStream.WriteAsync(bytes);
        response.OutputStream.Close();
    }

    private void Stop()
    {
        if (_listener.IsListening) _listener.Stop();
    }

    public ValueTask DisposeAsync()
    {
        Stop();
        _listener.Close();
        return ValueTask.CompletedTask;
    }
}

public interface ISystemBrowserLauncher
{
    void Open(string url);
}

public sealed class SystemBrowserLauncher : ISystemBrowserLauncher
{
    public void Open(string url)
    {
        try
        {
            Process.Start(new ProcessStartInfo { FileName = url, UseShellExecute = true });
        }
        catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            throw new AuthenticationFlowException("browser_launch", "Unable to open the system browser for authentication.");
        }
    }
}

public sealed record OidcLoginStatus(SessionInfo Session, bool Remembered, DateTimeOffset AccessTokenExpiry);

public sealed class OidcAuthenticationManager : IAsyncDisposable
{
    private readonly DesktopSession _session;
    private readonly ICredentialStore _refreshStore;
    private readonly IClientLogger _logger;
    private readonly ISystemBrowserLauncher _browser;
    private readonly SemaphoreSlim _refreshGate = new(1, 1);
    private CancellationTokenSource? _attempt;
    private OidcClient? _client;
    private Guid? _profileId;
    private string? _refreshToken;
    private int _generation;

    public AuthenticationUxState State { get; private set; } = AuthenticationUxState.AuthenticationRequired;
    public bool RememberedSession { get; private set; }
    public DateTimeOffset? TokenExpiry { get; private set; }
    public string LastErrorCategory { get; private set; } = "none";
    public static string CallbackMechanism => "loopback-127.0.0.1";
    public bool HasActiveSession => _profileId is not null;

    public OidcAuthenticationManager(DesktopSession session, ICredentialStore refreshStore, IClientLogger logger, ISystemBrowserLauncher? browser = null)
    {
        _session = session;
        _refreshStore = refreshStore;
        _logger = logger;
        _browser = browser ?? new SystemBrowserLauncher();
    }

    public async Task<OidcLoginStatus> SignInAsync(ServerProfile profile, OidcCapability capability, bool remember,
        Func<CancellationToken,Task<SessionInfo>> validateVmsSession, CancellationToken cancellationToken = default)
    {
        OidcCapabilityValidator.Validate(capability);
        CancelActiveAttempt();
        var generation = Volatile.Read(ref _generation);
        _attempt = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        State = AuthenticationUxState.Authenticating;
        LastErrorCategory = "none";
        _logger.Info("auth", $"organization sign-in started profile_id={profile.Id:D}");
        try
        {
            await using var callback = LoopbackCallbackListener.Create();
            var client = CreateClient(capability, callback.RedirectUri);
            var nonce = RandomToken(32);
            var parameters = new Parameters { { "nonce", nonce } };
            var prepared = await client.PrepareLoginAsync(parameters, _attempt.Token);
            if (prepared.IsError) throw new AuthenticationFlowException("oidc_prepare", "Unable to prepare organization sign-in.");
            if (!Uri.TryCreate(prepared.StartUrl, UriKind.Absolute, out var startUri))
                throw new AuthenticationFlowException("oidc_request", "Identity-provider authorization URL is invalid.");
            OidcAuthorizationRequestValidator.ValidatePrepared(startUri, callback.RedirectUri, prepared.State, prepared.CodeVerifier, nonce);
            if (generation != Volatile.Read(ref _generation)) throw new OperationCanceledException(_attempt.Token);
            _browser.Open(prepared.StartUrl);
            var responseData = await callback.WaitAsync(prepared.State, TimeSpan.FromMinutes(3), _attempt.Token);
            if (generation != Volatile.Read(ref _generation)) throw new OperationCanceledException(_attempt.Token);
            var result = await client.ProcessResponseAsync(responseData, prepared, cancellationToken: _attempt.Token);
            if (result.IsError)
                throw new AuthenticationFlowException(result.Error == "access_denied" ? "cancelled" : "oidc_response", "Organization sign-in was not completed.");
            var returnedNonce = result.User?.FindFirst("nonce")?.Value ?? "";
            if (!FixedEquals(returnedNonce, nonce))
                throw new AuthenticationFlowException("nonce_mismatch", "Identity response nonce validation failed.");
            if (string.IsNullOrWhiteSpace(result.AccessToken))
                throw new AuthenticationFlowException("token_response", "Identity provider did not return a usable access token.");

            var session = await _session.AuthenticateAsync(profile.Id, result.AccessToken, false, validateVmsSession, _attempt.Token);
            _client = client;
            _profileId = profile.Id;
            _refreshToken = string.IsNullOrWhiteSpace(result.RefreshToken) ? null : result.RefreshToken;
            TokenExpiry = result.AccessTokenExpiration;
            RememberedSession = remember && _refreshToken is not null;
            if (RememberedSession) await _refreshStore.SaveAsync(profile.Id, _refreshToken!, _attempt.Token);
            else await _refreshStore.DeleteAsync(profile.Id, _attempt.Token);
            State = AuthenticationUxState.Authenticated;
            _logger.Info("auth", $"organization sign-in completed profile_id={profile.Id:D} remembered={RememberedSession}");
            return new OidcLoginStatus(session, RememberedSession, result.AccessTokenExpiration);
        }
        catch (OperationCanceledException)
        {
            LastErrorCategory = "cancelled";
            State = AuthenticationUxState.AuthenticationRequired;
            throw;
        }
        catch (AuthenticationFlowException ex)
        {
            LastErrorCategory = ex.Category;
            State = AuthenticationUxState.Failed;
            _logger.Warning("auth", $"organization sign-in failed category={ex.Category}");
            throw;
        }
        catch (Exception ex)
        {
            LastErrorCategory = "identity_unreachable";
            State = AuthenticationUxState.Failed;
            _logger.LogError("auth", "organization sign-in failed", ex);
            throw new AuthenticationFlowException("identity_unreachable", "Unable to contact the identity provider.");
        }
        finally
        {
            _attempt?.Dispose();
            _attempt = null;
        }
    }

    public async Task<bool> TryRestoreAsync(ServerProfile profile, OidcCapability capability,
        Func<CancellationToken,Task<SessionInfo>> validateVmsSession, CancellationToken cancellationToken = default)
    {
        if (!capability.Enabled) return false;
        var refresh = await _refreshStore.LoadAsync(profile.Id, cancellationToken);
        if (string.IsNullOrWhiteSpace(refresh)) return false;
        OidcCapabilityValidator.Validate(capability);
        State = AuthenticationUxState.Refreshing;
        try
        {
            var client = CreateClient(capability, "http://127.0.0.1/oidc/callback/");
            var result = await client.RefreshTokenAsync(refresh, cancellationToken: cancellationToken);
            if (result.IsError)
            {
                if (IsPermanentRefreshError(result.Error)) await _refreshStore.DeleteAsync(profile.Id, cancellationToken);
                State = IsPermanentRefreshError(result.Error) ? AuthenticationUxState.Expired : AuthenticationUxState.OfflineWithRestorableSession;
                LastErrorCategory = IsPermanentRefreshError(result.Error) ? "refresh_rejected" : "refresh_unavailable";
                return false;
            }
            await _session.AuthenticateAsync(profile.Id, result.AccessToken, false, validateVmsSession, cancellationToken);
            _client = client; _profileId = profile.Id;
            _refreshToken = string.IsNullOrWhiteSpace(result.RefreshToken) ? refresh : result.RefreshToken;
            TokenExpiry = result.AccessTokenExpiration; RememberedSession = true; State = AuthenticationUxState.Authenticated; LastErrorCategory = "none";
            await _refreshStore.SaveAsync(profile.Id, _refreshToken, cancellationToken);
            return true;
        }
        catch (HttpRequestException)
        {
            State = AuthenticationUxState.OfflineWithRestorableSession; LastErrorCategory = "identity_unreachable"; return false;
        }
        catch (Exception ex)
        {
            State = AuthenticationUxState.OfflineWithRestorableSession; LastErrorCategory = "refresh_unavailable";
            _logger.LogError("auth","remembered session restore failed",ex); return false;
        }
    }

    public async Task<bool> TryRefreshAsync(CancellationToken cancellationToken = default)
    {
        if (_client is null || _profileId is null || string.IsNullOrWhiteSpace(_refreshToken)) return false;
        await _refreshGate.WaitAsync(cancellationToken);
        try
        {
            State = AuthenticationUxState.Refreshing;
            var result = await _client.RefreshTokenAsync(_refreshToken, cancellationToken: cancellationToken);
            if (result.IsError)
            {
                if (IsPermanentRefreshError(result.Error))
                {
                    await _refreshStore.DeleteAsync(_profileId.Value, cancellationToken);
                    _refreshToken = null; RememberedSession = false; State = AuthenticationUxState.Expired; LastErrorCategory = "refresh_rejected";
                }
                else { State = AuthenticationUxState.Failed; LastErrorCategory = "refresh_unavailable"; }
                return false;
            }
            _session.ReplaceAccessToken(_profileId.Value, result.AccessToken);
            _refreshToken = string.IsNullOrWhiteSpace(result.RefreshToken) ? _refreshToken : result.RefreshToken;
            TokenExpiry = result.AccessTokenExpiration; State = AuthenticationUxState.Authenticated; LastErrorCategory = "none";
            if (RememberedSession) await _refreshStore.SaveAsync(_profileId.Value, _refreshToken!, cancellationToken);
            return true;
        }
        catch (HttpRequestException)
        {
            State = AuthenticationUxState.OfflineWithRestorableSession; LastErrorCategory = "identity_unreachable"; return false;
        }
        catch (Exception ex)
        {
            State = AuthenticationUxState.Failed; LastErrorCategory = "refresh_unavailable";
            _logger.LogError("auth","session refresh failed",ex); return false;
        }
        finally { _refreshGate.Release(); }
    }

    public async Task InvalidateAsync(Guid profileId, CancellationToken cancellationToken = default)
    {
        CancelActiveAttempt();
        await _refreshStore.DeleteAsync(profileId, cancellationToken);
        ClearRuntime(AuthenticationUxState.Expired);
    }

    public async Task LogoutAsync(Guid profileId, CancellationToken cancellationToken = default)
    {
        CancelActiveAttempt();
        State = AuthenticationUxState.SigningOut;
        await _refreshStore.DeleteAsync(profileId, cancellationToken);
        ClearRuntime(AuthenticationUxState.AuthenticationRequired);
        _logger.Info("auth", $"local organization session signed out profile_id={profileId:D}");
    }

    public void Deactivate()
    {
        CancelActiveAttempt();
        ClearRuntime(AuthenticationUxState.Disconnected);
    }

    public void CancelActiveAttempt()
    {
        Interlocked.Increment(ref _generation);
        _attempt?.Cancel();
    }

    private void ClearRuntime(AuthenticationUxState state)
    {
        _client = null; _profileId = null; _refreshToken = null; TokenExpiry = null; RememberedSession = false; State = state;
    }

    private static OidcClient CreateClient(OidcCapability capability, string redirectUri)
    {
        var options = new OidcClientOptions
        {
            Authority = capability.Authority,
            ClientId = capability.ClientId,
            Scope = string.Join(' ', capability.Scopes),
            RedirectUri = redirectUri,
            BrowserTimeout = TimeSpan.FromMinutes(3),
            DisablePushedAuthorization = true,
        };
        options.FilteredClaims.Remove("nonce");
        return new OidcClient(options);
    }

    private static string RandomToken(int bytes) => OidcAuthorizationRequestValidator.Base64Url(RandomNumberGenerator.GetBytes(bytes));
    private static bool FixedEquals(string left, string right) => left.Length == right.Length &&
        CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(left), Encoding.UTF8.GetBytes(right));
    private static bool IsPermanentRefreshError(string? error) => error is "invalid_grant" or "invalid_client" or "unauthorized_client";

    public ValueTask DisposeAsync()
    {
        CancelActiveAttempt();
        _refreshGate.Dispose();
        _attempt?.Dispose();
        return ValueTask.CompletedTask;
    }
}
