# Active-Tile Live Stream Roles

**Milestone:** #285  
**Parent:** #221  
**Feature:** F02-012  
**Product status:** Release Candidate / External Qualification Pending

## Role model

MAIN, SUB and THIRD are camera stream roles, not fabricated quality levels. MAIN is always available because a managed camera requires a main path. SUB is offered only when `sub_path` is configured. THIRD is offered only when both the optional third path and stable third stream key exist.

The legacy primary live path is preserved: it pulls SUB when configured, otherwise MAIN. Therefore the default live role remains SUB-if-present, otherwise MAIN. When SUB exists, explicit MAIN viewing uses a deterministic auxiliary MediaMTX path derived from the existing live stream key. THIRD continues to use its existing stable third path. No transcoding or fourth role is introduced.

The camera API exposes `available_live_roles`; the browser does not infer THIRD or construct arbitrary MediaMTX paths.

## Active tile and switching

The #284 active tile owns the selector. Different cameras can use different roles concurrently. The duplicate-camera rule is unchanged, so one camera cannot consume multiple tiles merely by selecting different roles.

Role selection is client-memory viewer preference only. A switch invalidates the tile generation, closes/deletes the old WHEP session, requests a new exact-role grant and installs the new peer only if camera, role and generation are still current. The first implementation intentionally uses break-before-make: a failed requested role leaves that tile failed and explicitly retryable/role-selectable; it never silently falls back.

There is no automatic/adaptive switching or reconnect loop.

## Authorization and resource boundaries

Every role request passes the existing authorized-camera tenant/site boundary. The control API maps the requested role to an authoritative configured path and mints the existing #278 short-lived RS256 grant for exactly that MediaMTX path. #282 key rotation is unchanged. Tokens remain memory-only and Authorization-header-only, and WHEP Location remains same-origin validated.

Every provisioned viewer path uses #281 `maxReaders`. Explicit MAIN is a live path and receives the same safety ceiling. Recording paths do not.

## Recording and AI invariants

Recording remains independent. Continuous recording still uses the camera MAIN source through its separate `record_stream_key`, `record=true`, `sourceOnDemand=false` path and does not inherit live `maxReaders`. Changing a tile to MAIN/SUB/THIRD does not mutate recording policy, retention, placement, fencing or completion hooks.

AI policy remains a separately persisted camera configuration with its own `stream_role`. Viewer role selection does not write AI policy or change inference/event metadata sources.

## Reconfiguration

Camera/ONVIF profile reassignment remains camera configuration. Source mutation/reconciliation refreshes the viewer paths from the new persisted MAIN/SUB/THIRD mapping. Removing SUB removes the no-longer-needed explicit MAIN auxiliary path because the legacy live path itself then points to MAIN. Removing THIRD continues to remove its optional path.

## Qualification boundary

Software tests can prove role availability, path/grant selection, lifecycle fencing, reader policy and recording/AI separation. They do not prove physical camera three-stream support, codec/resolution/frame-rate behavior, browser decode capacity, TURN/NAT/WAN behavior, switch latency, failover continuity or soak. Those remain NV / External Qualification Pending.
