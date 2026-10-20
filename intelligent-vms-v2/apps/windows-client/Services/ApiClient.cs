using System.Net;
using System.Net.Http;
using IntelligentVms.Client.Models;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace IntelligentVms.Client.Services;

/// <summary>Problem document returned by every Intelligent VMS endpoint.</summary>
public sealed record Problem(
    [property: JsonPropertyName("title")] string Title,
    [property: JsonPropertyName("status")] int Status,
    [property: JsonPropertyName("code")] string Code,
    [property: JsonPropertyName("detail")] string? Detail,
    [property: JsonPropertyName("correlation_id")] string? CorrelationId);

/// <summary>
/// Thrown for any non-success response. Callers branch on <see cref="Problem.Code"/>,
/// which is a stable token; the message text is for humans only.
/// </summary>
public sealed class VmsApiException(Problem problem)
    : Exception(problem.Detail ?? problem.Title)
{
    public Problem Problem { get; } = problem;
    public string Code => Problem.Code;
}

public sealed record TokenPair(
    [property: JsonPropertyName("access_token")] string AccessToken,
    [property: JsonPropertyName("refresh_token")] string RefreshToken,
    [property: JsonPropertyName("expires_in")] int ExpiresIn);

/// <summary>
/// Typed client for the control API.
///
/// Tokens are held in memory only. A 401 triggers exactly one refresh attempt
/// (guarded by a semaphore so concurrent calls cannot each rotate the refresh
/// token and trip the server's token-reuse detection), after which the
/// original request is replayed once.
/// </summary>
public sealed class ApiClient : IDisposable
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private readonly HttpClient _http;
    private readonly SemaphoreSlim _refreshGate = new(1, 1);
    private TokenPair? _tokens;

    public ApiClient(Uri baseAddress, HttpMessageHandler? handler = null)
    {
        _http = handler is null ? new HttpClient() : new HttpClient(handler);
        _http.BaseAddress = new Uri(baseAddress, "/api/v1/");
        _http.Timeout = TimeSpan.FromSeconds(30);
        _http.DefaultRequestHeaders.UserAgent.ParseAdd("IntelligentVms-WpfClient/0.1.0");
    }

    public bool IsAuthenticated => _tokens is not null;

    public async Task LoginAsync(string username, string password,
                                 string? totpCode = null,
                                 CancellationToken cancellationToken = default)
    {
        var response = await _http.PostAsJsonAsync(
            "auth/login",
            new { username, password, totp_code = totpCode },
            JsonOptions, cancellationToken).ConfigureAwait(false);

        _tokens = await ReadAsync<TokenPair>(response, cancellationToken).ConfigureAwait(false);
    }

    public async Task LogoutAsync(CancellationToken cancellationToken = default)
    {
        if (_tokens is null) return;
        try
        {
            using var request = Build(HttpMethod.Post, "auth/logout",
                new { refresh_token = _tokens.RefreshToken });
            await _http.SendAsync(request, cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            _tokens = null;
        }
    }

    public Task<T> GetAsync<T>(string path, CancellationToken cancellationToken = default)
        => SendAsync<T>(HttpMethod.Get, path, null, cancellationToken);

    public Task<T> PostAsync<T>(string path, object? body = null,
                                CancellationToken cancellationToken = default)
        => SendAsync<T>(HttpMethod.Post, path, body, cancellationToken);

    public Task<T> DeleteAsync<T>(string path, CancellationToken cancellationToken = default)
        => SendAsync<T>(HttpMethod.Delete, path, null, cancellationToken);

    public Task<CameraPage> ListCamerasAsync() => GetAsync<CameraPage>("cameras?limit=500");

    public Task<LiveSession> OpenLiveSessionAsync(string cameraId, int tileWidth, int tileHeight) =>
        PostAsync<LiveSession>("live/sessions", new { camera_id = cameraId, tile_width = tileWidth, tile_height = tileHeight });

    public Task<JsonElement> KeepAliveAsync(string sessionId) =>
        PostAsync<JsonElement>($"live/sessions/{Uri.EscapeDataString(sessionId)}/keepalive");

    public Task<JsonElement> CloseLiveSessionAsync(string sessionId) =>
        DeleteAsync<JsonElement>($"live/sessions/{Uri.EscapeDataString(sessionId)}");

    private async Task<T> SendAsync<T>(HttpMethod method, string path, object? body,
                                       CancellationToken cancellationToken)
    {
        using var first = Build(method, path, body);
        var response = await _http.SendAsync(first, cancellationToken).ConfigureAwait(false);

        if (response.StatusCode == HttpStatusCode.Unauthorized && _tokens is not null)
        {
            response.Dispose();
            await RefreshAsync(cancellationToken).ConfigureAwait(false);
            using var retry = Build(method, path, body);
            response = await _http.SendAsync(retry, cancellationToken).ConfigureAwait(false);
        }

        return await ReadAsync<T>(response, cancellationToken).ConfigureAwait(false);
    }

    private HttpRequestMessage Build(HttpMethod method, string path, object? body)
    {
        var request = new HttpRequestMessage(method, path);
        if (_tokens is not null)
        {
            request.Headers.Authorization =
                new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", _tokens.AccessToken);
        }
        if (body is not null)
        {
            request.Content = JsonContent.Create(body, options: JsonOptions);
        }
        return request;
    }

    private async Task RefreshAsync(CancellationToken cancellationToken)
    {
        await _refreshGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            // Another thread may already have refreshed while we waited.
            var current = _tokens ?? throw new InvalidOperationException("not signed in");
            using var request = new HttpRequestMessage(HttpMethod.Post, "auth/refresh")
            {
                Content = JsonContent.Create(new { refresh_token = current.RefreshToken },
                                             options: JsonOptions),
            };
            var response = await _http.SendAsync(request, cancellationToken).ConfigureAwait(false);
            _tokens = await ReadAsync<TokenPair>(response, cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            _refreshGate.Release();
        }
    }

    private static async Task<T> ReadAsync<T>(HttpResponseMessage response,
                                              CancellationToken cancellationToken)
    {
        using (response)
        {
            if (response.StatusCode == HttpStatusCode.NoContent)
            {
                return default!;
            }

            var payload = await response.Content.ReadAsStringAsync(cancellationToken)
                                                .ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
            {
                var problem = TryDeserialize<Problem>(payload)
                    ?? new Problem(response.ReasonPhrase ?? "Request failed",
                                   (int)response.StatusCode, "unknown_error", payload, null);
                throw new VmsApiException(problem);
            }

            return JsonSerializer.Deserialize<T>(payload, JsonOptions)!;
        }
    }

    private static T? TryDeserialize<T>(string payload)
    {
        try { return JsonSerializer.Deserialize<T>(payload, JsonOptions); }
        catch (JsonException) { return default; }
    }

    public void Dispose()
    {
        _http.Dispose();
        _refreshGate.Dispose();
    }
}
