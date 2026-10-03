# Multi-Camera Live-View Workspace

**Milestone:** #283  
**Parent:** #221  
**Product status:** Release Candidate / External Qualification Pending

## Scope

The browser workspace provides bounded fixed layouts: 1x1, 2x2, 3x3 and 4x4. These are UI choices, not measured workstation, decoder, GPU, network or MediaMTX capacity claims. Camera assignments are client-memory state only; saved views are outside this milestone.

Only cameras returned by the caller's authorized camera inventory can be selected. Accidental duplicate camera assignments are prevented so one operator action does not silently consume multiple readers for the same camera.

## Tile lifecycle

Each assigned tile owns one independent WHEP lifecycle. The UI represents empty/stopped, authorization/loading, connecting, live and failed states. A failed tile does not intentionally tear down another tile.

Camera replacement, tile removal, layout shrink and page exit invalidate the tile generation, close its RTCPeerConnection, clear media references/listeners and attempt best-effort authenticated WHEP DELETE. Cleanup is idempotent. MediaMTX remains authoritative when browser cleanup cannot run.

Every asynchronous connection attempt is generation-bound. Late grant, WHEP or WebRTC completion from an older camera assignment is ignored and any known stale WHEP session is deleted rather than installed into the current tile.

Retry after failure is explicit/manual in this first milestone. There is no automatic infinite reconnect loop, which avoids a multi-tile camera outage becoming a grant/WHEP retry storm.

## Security

The workspace reuses #278/#282 live access unchanged. Bearer grants remain short-lived, path-scoped and in memory. They are sent only in Authorization headers for WHEP OPTIONS/POST/DELETE. WHEP Location must remain on the authorized media origin. Grants are not placed in query strings, DOM text, browser storage or analytics.

Camera/site labels are escaped before HTML rendering. Public live endpoints remain credential-free. RTSP credentials and private signing keys never enter the browser.

## Resource and recording boundaries

MediaMTX #281 maxReaders remains the server-side live-reader safety boundary. A reader rejection is surfaced as a tile failure and is never bypassed. The workspace adds no second control-plane quota database.

Grid operations do not mutate recording policy or recording MediaMTX paths. Continuous recording remains main/source-stream, non-on-demand and independent of live tile lifecycle. AI source selection is unchanged.

## Qualification boundary

Deterministic CI validates client state/lifecycle/security contracts and recording separation. It does not prove real camera/browser codec compatibility, TURN/NAT/WAN behavior, sixteen-stream workstation/GPU capacity, MediaMTX deployment capacity, failover visual continuity, cleanup latency after browser crashes, or long-duration soak. Those remain NV / External Qualification Pending.
