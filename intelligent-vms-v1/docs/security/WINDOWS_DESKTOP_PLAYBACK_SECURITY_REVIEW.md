# Windows Desktop Playback Security Review

**Milestone:** issue #12 / PR #13  
**Boundary:** deterministic desktop playback software evidence.

| Review item | Mitigation |
| --- | --- |
| Playback authorization | Timeline/play/export remain existing authenticated VMS APIs; media start performs session preflight with bounded refresh |
| Tenant/site isolation | Server `authorized_camera` remains authoritative |
| Direct DB/filesystem access | Desktop has none |
| Direct RTSP / second backend | No playback RTSP path or alternate recorder/player backend |
| Media credential leakage | Playback URL contains no bearer; native host injects Authorization header only for same VMS origin/path |
| Cross-profile reuse | Playback coordinator is destroyed/cleared before profile/server authority changes |
| Stale seek/open | Coordinator generation + session IDs and renderer generation fence late completions |
| Timeline truth | Only server recording spans are rendered; gaps are preserved |
| Query load | Desktop requests one bounded selected day; server maximum-window/segment limits remain enforced |
| Export authorization | Existing authorized server export endpoint reused; no server filesystem path is exposed |
| WebView2 navigation | Packaged virtual-host page only; arbitrary navigation/new windows/permissions/host objects blocked |
| Logging/diagnostics | Safe identifiers/state/counts only; no media URLs/tokens/filesystem paths |
| Logout/session expiry | Protected playback context and renderer are torn down |
| App shutdown | Playback coordinator and renderer are stopped/disposed |
| Retention race | A removed recording returns unavailable/failure; stale success is not fabricated |

## Result

PASS at the software-review boundary subject to final exact-head CI/review. No second playback authority, server filesystem exposure, database access, RTSP credential path, token-in-URL, cross-profile media persistence or fabricated recording continuity is introduced.

External codec/device/storage/Windows/performance qualification remains pending.
