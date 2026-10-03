# Phase 2A ONVIF interoperability matrix

Status: Research Agent handoff to Architect/Developer.

## Goal

Implement camera onboarding without requiring the operator to know RTSP paths, while keeping manual RTSP onboarding as a fallback.

## Protocol sequence

| Step | ONVIF mechanism | VMS use | Required for Phase 2A | Fallback |
|---|---|---|---|---|
| LAN discovery | WS-Discovery Probe / ProbeMatch | Find ONVIF device service XAddr | Optional because multicast may not traverse VLANs | Manual IP/host entry |
| Device identity | Device.GetDeviceInformation | manufacturer/model/firmware/serial/hardware | Yes | Store unknown fields; do not block streaming |
| Service map | Device.GetServices(IncludeCapability=true) | Locate Media/PTZ/Events/etc | Yes | GetCapabilities for older devices |
| Base capabilities | Device.GetCapabilities | Backward-compatible service addresses | Yes | Use service XAddrs discovered via GetServices |
| Media profiles | Media.GetProfiles | Enumerate stream configurations | Yes | Vendor adapter/manual RTSP if broken |
| Stream URI | Media.GetStreamUri | Obtain RTSP URI for selected profile | Yes | Manual RTSP path |
| PTZ | PTZ service presence + profile config | Capability flag | No | false/unknown |
| Imaging | Imaging service presence | Capability flag | No | false/unknown |
| Events | Events service presence | Future event subscription | No | false/unknown |
| Edge recording | Recording/Search/Replay services | Profile-G-related capability | No | false/unknown |
| Analytics metadata | Analytics/metadata configuration and later event/metadata probes | Profile-M-related capability | No | false/unknown |
| Media2 | Media2 service | Newer media configuration, H.265-rich devices | Opportunistic | Media v1 first in Phase 2A |

## Important interpretation rule

Do not claim that a camera is ONVIF Profile T/G/M conformant merely because a related service exists. Profile conformance is a product claim verified through ONVIF conformance. The VMS stores observed capabilities separately from declared/verified profile information.

## Authentication

Phase 2A supports:
- WS-Security UsernameToken PasswordDigest in SOAP.
- HTTP Digest authentication as a compatibility aid when the device challenges at HTTP level.

Credentials:
- are accepted only over the VMS API;
- are encrypted at rest in the prototype;
- are never returned in public camera/capability responses;
- must never be logged in raw form;
- should move to Vault/KMS in production.

## Discovery realities

WS-Discovery uses link-local multicast and commonly fails across routed VLANs, container bridge networks, MPLS boundaries and firewalls. Therefore discovery is a convenience, not a dependency.

Production topology:
- run discovery/probe workers close to cameras, per site/region;
- use bounded worker pools;
- send normalized results to the central control plane;
- never attempt one broadcast discovery operation for 100K cameras.

## Stream selection

Phase 2A normalizes each media profile:
- token
- name
- encoding
- width/height
- frame rate when present
- bitrate limit when present
- sanitized RTSP URI

Default policy:
- main = highest pixel count, tie-break by bitrate/fps;
- sub = lowest distinct usable profile;
- operator/API can override profile token.

Never assume profile order equals main/sub.

## Security notes

ONVIF probe targets are SSRF-sensitive because the VMS makes outbound HTTP requests. Phase 2A therefore restricts probe hosts to configurable camera/private CIDRs by default. Public addressing can be enabled explicitly only where deployment requires it.

XML is parsed with defusedxml and bounded response sizes/timeouts. Do not expand external entities or trust arbitrary XAddr hosts without policy validation.

## Vendor fallback policy

Vendor-specific code is allowed only behind an adapter interface and only after a real interoperability failure is reproduced and covered by a test. Standard ONVIF remains the first path.

## Research conclusion

The minimum reliable Phase 2A onboarding flow is:

WS-Discovery (optional) -> device service -> GetDeviceInformation -> GetServices/GetCapabilities -> Media.GetProfiles -> Media.GetStreamUri -> normalized capability/profile record -> VMS camera record -> MediaMTX provisioning.

Profile T is the primary advanced-streaming target; Profile G is relevant to later edge-recording retrieval; Profile M is relevant to later analytics metadata/events.
