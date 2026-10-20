# Intelligent VMS Windows client

A **native .NET 8 / WPF** application using MVVM — not a browser wrapper. The
requirement (M13-R01) is explicit on that point, and the architecture honours
it: video is rendered by LibVLCSharp into native surfaces, windows are real
WPF windows that can be moved across monitors, and there is no embedded
browser anywhere in the codebase.

## Build status — read this first

**This client has never been compiled.** No .NET SDK was available in the
environment that produced this source tree. `dotnet build` is Gate 4 of
`docs/verification/external-qualification-plan.md`. Every Module 13
requirement is therefore marked *"Partial — source delivered, not built"* in
the feature matrix rather than claimed as working.

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

**Credentials never reach this process.** The client receives a gateway path
and a short-lived play token. It never sees an RTSP URL or a camera password.

**PTZ safety.** The client refreshes its PTZ control lock while a button is
held. If this process dies mid-movement, the lock expires and the server's
watchdog stops the camera — the client is not trusted to be the only safeguard.
