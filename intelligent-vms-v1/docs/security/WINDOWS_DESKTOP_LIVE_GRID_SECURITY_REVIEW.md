# Windows Desktop Live Grid Security Review

**Milestone:** issue #10 / PR #11
**Boundary:** native Windows live-grid software evidence only.

| Review item | Mitigation | Residual / external boundary |
| --- | --- | --- |
| Per-tile authorization | Every start uses the existing authenticated VMS live-grant API for the assigned authorized camera | Real deployment/policy qualification external |
| Media-grant isolation | Grants stay inside renderer start calls and WHEP Authorization headers | Host memory compromise external |
| Direct RTSP bypass | Grid/coordinator have no camera RTSP path or credential store | Camera/vendor interoperability external |
| Duplicate reader abuse | Accidental duplicate camera assignment is rejected | Multi-user/global quota remains deployment policy |
| Stale completion | Per-tile generation/cancellation plus existing controller fencing | External provider may delay cleanup but cannot install stale state |
| Profile/server isolation | Grid coordinator is profile-bound and disposed on switch | None known in software boundary |
| Auth expiry | Session expiry tears down the coordinator and authorized UI | Peer cleanup timing external |
| Hidden sessions | Layout shrink clears hidden sessions/assignments; focus stops non-focused sessions | Process-crash cleanup timing external |
| WHEP lifecycle | Authorization header for WHEP and DELETE/peer close on stop | Runtime crash can prevent explicit DELETE |
| Cross-origin WHEP | Renderer rejects a WHEP Location from a different origin | Compromised authorized media origin external |
| Token in URL | Existing renderer rejects unsafe/query-bearing media base and does not place grants in URLs | Runtime internals external |
| Logging | Safe tile/camera identifiers and roles only; bounded logger redacts named secrets | OS crash dump external |
| Diagnostics | Layout/count/state/role summaries only, never grants/URLs/tokens | Operator filesystem access external |
| Persistence | live-grid.json stores profile/layout/tile camera IDs only | Camera IDs are metadata, not credentials |
| WebView2 boundary | Local virtual host only; new windows blocked; permissions denied; host objects disabled | Platform vulnerabilities external |
| Failure isolation | Tile grant/renderer failure stays tile-local; auth failure is central | Server outage affects all media by nature |
| Shutdown/profile cleanup | Stop-all, fencing, and renderer disposal on window shutdown | Hard process kill timing external |

## Result

PASS at deterministic software-review level after implementation/regression coverage. No recorder authority, camera credential path, direct RTSP playback, token persistence, token query string, unsafe WebView2 navigation, or cross-profile grid reuse is introduced.

The WebView2-per-tile renderer is explicitly a foundation limitation, not a capacity result. Real camera/codec, Windows 10/11, resource, WAN, and long-duration qualification remain External Qualification Pending.
