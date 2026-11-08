# Firewall and Network Design

## Ports

| Port | Protocol | Direction | Purpose | Expose to |
| --- | --- | --- | --- | --- |
| 443 | TCP | Inbound | HTTPS (reverse proxy) | Client network |
| 8080 | TCP | Inbound | Control API | **Loopback only** behind a proxy |
| 8889 | TCP | Inbound | WebRTC signalling (WHEP) | Client network |
| 8189 | UDP | Inbound | WebRTC ICE media | Client network |
| 8888 | TCP | Inbound | HLS fallback | Client network |
| 8554 | TCP | Inbound | RTSP for native clients | Client network (optional) |
| 9997 | TCP | Internal | MediaMTX control API | **Loopback only** |
| 5432 | TCP | Internal | PostgreSQL | **Loopback or database subnet only** |
| 554 | TCP | Outbound | RTSP to cameras | Camera VLAN |
| 80/443 | TCP | Outbound | ONVIF to cameras | Camera VLAN |
| 3702 | UDP | Outbound | WS-Discovery multicast | Camera VLAN |

## Recommended topology

```
        Client network                    Camera VLAN (isolated)
              │                                   │
         ┌────▼─────┐                       ┌─────▼─────┐
         │  nginx   │ 443                   │  cameras  │
         └────┬─────┘                       └─────▲─────┘
              │ loopback                          │ 554 / 80
       ┌──────▼──────────────────────────────────┬┘
       │  VMS server                              │
       │   control-api (8080, loopback)           │
       │   mediamtx   (8889/8189/8888 to clients) │
       │   recorder   (outbound to cameras)       │
       └──────────────────────────────────────────┘
```

**Cameras must not be reachable from the client network.** The VMS is the only
bridge. This is the single most effective control in a CCTV deployment:
IP cameras are frequently unpatched, and direct client access to them bypasses
every permission, audit and privacy control the VMS provides.

## Hardening checklist

- [ ] Camera VLAN has no route to the client network
- [ ] `camera_allow_cidrs` set to the camera subnet (turns the SSRF guard from
      deny-list into allow-list)
- [ ] API bound to loopback, TLS terminated at the proxy
- [ ] `trusted_hosts` set to real hostnames, never `*`
- [ ] PostgreSQL not listening on a public interface
- [ ] MediaMTX control API (9997) on loopback only
- [ ] Outbound internet access not required — the product runs fully offline
- [ ] Default camera passwords changed before onboarding
