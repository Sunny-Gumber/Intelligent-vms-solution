# ADR — Windows Desktop PTZ Control Foundation

Status: Accepted for software qualification in Issue #14 / PR #15  
Product status: Release Candidate / External Qualification Pending

## Decision

The native Windows client controls PTZ only through the authenticated Intelligent VMS Control API. The desktop never receives ONVIF credentials, PTZ service addresses, RTSP camera endpoints or raw SOAP. The Control API re-authorizes the camera for the current tenant/site, decrypts credentials only server-side, applies camera-network target policy, validates normalized input, and performs bounded ONVIF device calls.

The foundation supports capability-driven continuous pan/tilt and optical zoom when an ONVIF PTZ node advertises the corresponding continuous velocity space. It deliberately does not claim digital zoom, focus/iris, presets, tours, patterns, patrols, home position, auxiliary/vendor controls or hardware compatibility.

## Client ownership

`PtzCoordinator` owns exactly one selected live tile and one client-context UUID. It tracks active camera/tile, capability state, monotonic generation, pending cancellation, movement state and safe error category.

PTZ is available only while the selected tile is in `Live` state and the Live workspace is active. Selection/camera/layout/focus/workspace/profile/auth/app lifecycle changes fence the old generation and perform bounded best-effort STOP before relinquishing ownership.

No monitor is held across PTZ network I/O. A newer STOP therefore cancels/fences an in-flight MOVE immediately instead of waiting behind it.

## Server command safety

The server accepts normalized finite vectors in `[-1,1]`, rejects zero moves, unsupported axes, stale generations and movement bursts below the bounded interval. STOP is not movement-rate-limited.

Generation ownership is keyed by authenticated principal, camera and opaque client-context UUID. This avoids stale replay and fresh-client/restart generation collisions.

If an already-started device MOVE returns after a newer generation has won, the server sends a compensating STOP before reporting the MOVE as superseded.

## Dead-man contract

Press begins one continuous MOVE. Release or lost mouse capture sends STOP. STOP is also attempted on active tile/camera change, tile clear, layout/focus transition, leaving Live workspace, window deactivation, logout, authentication expiry, server-profile change, coordinator disposal and application shutdown.

A PTZ failure changes only PTZ state. It does not mutate recording policy, recorder ownership, live media grants, retention, AI, or unrelated live tiles.

## Capability truth

Software support and hardware verification are separate. `hardware_verified` remains false. Capability discovery is live/server-authoritative and does not infer support from camera model names.

Presets, focus and iris are not implemented because the accepted backend had no safe generic control abstraction for them.

## Security properties

- Authentication and admin/operator authorization are server-side.
- `authorized_camera` preserves tenant/site isolation.
- Camera credentials remain server-side.
- Site target pinning applies to PTZ service endpoints.
- Raw SOAP/device faults and credentials are not returned to the desktop.
- Existing bounded one-refresh/one-retry authentication behavior is reused.

## External qualification boundary

Normal CI uses deterministic fake PTZ services and does not establish physical PTZ behavior. Named manufacturer/model/firmware, ONVIF interoperability, real stop latency, zoom semantics and loss/reboot behavior remain External Qualification Pending.