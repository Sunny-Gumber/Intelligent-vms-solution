# Intelligent VMS Windows client

A **native .NET 8 / WPF** application using MVVM — not a browser wrapper. The
requirement (M13-R01) is explicit on that point, and the architecture honours
it: video is rendered by LibVLCSharp into native surfaces, windows are real
WPF windows that can be moved across monitors, and there is no embedded
browser anywhere in the codebase.

## Build status — read this first

**Build status:** the `net8.0-windows` target now builds in Release from Linux
with Windows targeting enabled. The application has not been run on Windows,
and video playback has not been exercised against a live gateway. A successful
compile does not qualify runtime behavior; Module 13 requirements remain
partial until their applicable checks in
`docs/verification/external-qualification-plan.md` pass.

## Building

```powershell
dotnet restore
dotnet build -c Release
dotnet run --project IntelligentVms.Client.csproj
```

## Structure

| Path | Purpose |
| --- | --- |
| `Services/ApiClient.cs` | Typed HTTP client, token refresh, correlation IDs |
| `Services/LayoutService.cs` | Saved layouts, multi-monitor placement |
| `ViewModels/` | MVVM view models (no UI types) |
| `Views/` | XAML views |
| `Controls/VideoTile.xaml.cs` | LibVLCSharp rendering surface |

## Design notes

**Tile isolation.** Each tile owns its own live session and its own error
state. A camera that fails shows an error in its own tile; the rest of the wall
keeps running, and no unrelated stream is restarted (M3-R06, M3-R10).

**Camera credentials never reach this process.** The client receives a gateway
path and a short-lived play token. It never sees an RTSP URL or camera password.
For HLS playback, LibVLC answers the gateway's HTTP Basic challenge with the
live session id and play token. Each tile owns its LibVLC instance so credentials
cannot cross between viewers; they are not stored in the media URL or persisted.

**PTZ safety.** The client refreshes its PTZ control lock while a button is
held. If this process dies mid-movement, the lock expires and the server's
watchdog stops the camera — the client is not trusted to be the only safeguard.
