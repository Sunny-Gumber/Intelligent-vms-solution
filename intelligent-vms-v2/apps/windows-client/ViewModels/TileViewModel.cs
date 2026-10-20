// One video tile. Owns exactly one live session and one player.
//
// Invariants:
//   * A tile never throws into the UI thread; errors become visible state.
//   * Reconnection is bounded and jittered, mirroring the server's policy.
//   * Stopping a tile always releases the server-side session, so a closed
//     tile does not consume one of the server's bounded session slots.

using System;
using System.Threading;
using System.Threading.Tasks;
using IntelligentVms.Client.Models;
using IntelligentVms.Client.Services;

namespace IntelligentVms.Client.ViewModels;

public sealed class TileViewModel : ViewModelBase
{
    private static readonly Random Jitter = new();

    private readonly ApiClient _api;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private LiveSession? _session;
    private CancellationTokenSource? _retryCts;
    private string _status = "Empty";
    private string? _error;
    private string? _cameraName;
    private int _attempt;

    public int Index { get; }
    public string? CameraId { get; private set; }

    public string Status { get => _status; private set => SetField(ref _status, value); }
    public string? Error { get => _error; private set => SetField(ref _error, value); }
    public string? CameraName { get => _cameraName; private set => SetField(ref _cameraName, value); }
    public string? StreamUrl => _session?.WhepUrl;
    public string? PlayToken => _session?.PlayToken;

    public TileViewModel(ApiClient api, int index)
    {
        _api = api;
        Index = index;
    }

    public async Task StartAsync(string cameraId)
    {
        await _gate.WaitAsync();
        try
        {
            CameraId = cameraId;
            Status = "Connecting";
            Error = null;
            _attempt = 0;
            await OpenSessionAsync();
        }
        finally
        {
            _gate.Release();
        }
    }

    private async Task OpenSessionAsync()
    {
        try
        {
            // The server chooses main/sub/third from the tile's pixel size, so
            // a thumbnail never pulls a 4K main stream.
            _session = await _api.OpenLiveSessionAsync(CameraId!, tileWidth: 640, tileHeight: 360);
            CameraName = _session.CameraName;
            Status = "Live";
            Error = null;
            _attempt = 0;
            OnPropertyChanged(nameof(StreamUrl));
            OnPropertyChanged(nameof(PlayToken));
        }
        catch (ApiException ex) when (ex.Code == "media_gateway_unavailable")
        {
            // Be precise: recording is unaffected, and the operator should know.
            SetError("Live video is unavailable (media gateway down). Recording continues.");
            ScheduleRetry();
        }
        catch (ApiException ex) when (ex.Code == "permission_denied")
        {
            SetError("You do not have access to this camera.");   // no retry
        }
        catch (ApiException ex) when (ex.Code is "live_session_limit" or "user_session_limit")
        {
            SetError(ex.Detail ?? "Session limit reached.");
            ScheduleRetry();
        }
        catch (Exception ex)
        {
            SetError(ex.Message);
            ScheduleRetry();
        }
    }

    private void ScheduleRetry()
    {
        _retryCts?.Cancel();
        _retryCts = new CancellationTokenSource();
        var token = _retryCts.Token;
        _attempt++;

        // Bounded exponential backoff with full jitter, capped at 60s.
        var ceiling = Math.Min(60_000, 1_000 * Math.Pow(2, Math.Min(_attempt, 6)));
        var delay = TimeSpan.FromMilliseconds(Jitter.NextDouble() * ceiling);

        _ = Task.Run(async () =>
        {
            try { await Task.Delay(delay, token); } catch (TaskCanceledException) { return; }
            if (!token.IsCancellationRequested && CameraId is not null)
            {
                Status = "Reconnecting";
                await OpenSessionAsync();
            }
        }, token);
    }

    public async Task KeepAliveAsync()
    {
        if (_session is null) return;
        await _api.KeepAliveAsync(_session.SessionId);
    }

    public void SetError(string message)
    {
        Error = message;
        Status = "Error";
    }

    public async Task StopAsync()
    {
        _retryCts?.Cancel();
        if (_session is not null)
        {
            try { await _api.CloseLiveSessionAsync(_session.SessionId); }
            catch { /* the server expires it anyway; never block teardown */ }
            _session = null;
        }
        CameraId = null;
        CameraName = null;
        Status = "Empty";
        Error = null;
        OnPropertyChanged(nameof(StreamUrl));
    }
}
