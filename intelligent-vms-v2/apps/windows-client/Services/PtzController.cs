using System.Windows.Threading;

namespace IntelligentVms.Client.Services;

/// <summary>
/// PTZ control with a client-side hold timer.
///
/// SAFETY NOTE: this timer is a convenience, NOT the safeguard. The server
/// clamps every movement to <c>ptz_max_move_seconds</c>, the ONVIF command
/// carries a device-side timeout, and a server watchdog stops any camera whose
/// movement window elapsed or whose controlling client vanished. If this
/// process is killed mid-pan, the camera still stops.
/// </summary>
public sealed class PtzController : IDisposable
{
    private readonly ApiClient _api;
    private readonly DispatcherTimer _holdTimer;
    private string? _cameraId;
    private (double Pan, double Tilt, double Zoom, double Speed) _vector;

    public PtzController(ApiClient api)
    {
        _api = api;
        // Re-issue while held so the server's lock stays fresh. The interval is
        // shorter than the server's lock TTL.
        _holdTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(3) };
        _holdTimer.Tick += async (_, _) => await SendAsync().ConfigureAwait(false);
    }

    public async Task StartMoveAsync(string cameraId, double pan, double tilt,
                                     double zoom, double speed = 0.5)
    {
        _cameraId = cameraId;
        _vector = (pan, tilt, zoom, speed);
        await SendAsync().ConfigureAwait(false);
        _holdTimer.Start();
    }

    public async Task StopAsync()
    {
        _holdTimer.Stop();
        if (_cameraId is null) return;

        var cameraId = _cameraId;
        _cameraId = null;
        await _api.PostAsync<object>($"ptz/{cameraId}/command", new { kind = "stop" })
                  .ConfigureAwait(false);
    }

    private async Task SendAsync()
    {
        if (_cameraId is null) return;
        try
        {
            await _api.PostAsync<object>($"ptz/{_cameraId}/command", new
            {
                kind = "continuous",
                pan = _vector.Pan,
                tilt = _vector.Tilt,
                zoom = _vector.Zoom,
                speed = _vector.Speed,
            }).ConfigureAwait(false);
        }
        catch (VmsApiException error) when (error.Code == "ptz_locked")
        {
            // Another operator holds control. Stop trying; the UI surfaces who.
            _holdTimer.Stop();
            throw;
        }
        catch (VmsApiException error) when (error.Code == "ptz_capability_unavailable")
        {
            // The camera never demonstrated PTZ support. Do not retry.
            _holdTimer.Stop();
            throw;
        }
    }

    public void Dispose() => _holdTimer.Stop();
}
