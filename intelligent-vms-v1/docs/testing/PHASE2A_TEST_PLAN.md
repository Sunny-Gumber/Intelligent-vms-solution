# Phase 2A QA plan

Status: QA Agent test plan and current evidence.

## Automated unit gates

- private vs public ONVIF target policy
- WS-Discovery ProbeMatch parsing
- media profile parsing
- main/sub selection independent of returned order
- RTSP credential injection with URL escaping
- public probe output cannot expose internal raw URI
- existing capacity/RTSP tests remain present

## Real-device integration matrix

Each camera under test records manufacturer/model/firmware and whether ONVIF conformance is independently verified.

| Case | Expected |
|---|---|
| valid credentials | identity + profiles + sanitized URIs returned |
| invalid credentials | AUTH_FAILED, no password in response/log |
| camera offline | bounded timeout, NETWORK_UNREACHABLE |
| wrong ONVIF port/path | stable structured failure |
| single profile | main selected, sub null |
| many profiles in arbitrary order | highest-resolution main, lowest distinct sub default |
| H.264 main/sub | provision selected view stream |
| H.265 main + H.264 sub | sub preferred for browser live view |
| no sub profile | main used for live view |
| PTZ unsupported | feature false/unknown, onboarding still works |
| events unsupported | feature false/unknown, onboarding still works |
| MediaMTX unavailable | DB onboarding transaction rolled back |
| duplicate ONVIF XAddr within tenant/site | HTTP 409 DUPLICATE_CAMERA |
| capability refresh | snapshot timestamp/data updated without exposing credentials |

## Discovery caveat

WS-Discovery is link-local multicast. Docker bridge, VLAN boundaries or cloud networks may hide discovery traffic. A discovery failure is not a camera failure. Manual-IP probe/onboard must always be tested separately.

## Security tests

1. Public target (e.g. 8.8.8.8) is blocked with default network policy.
2. Device/service XAddr outside allowed CIDRs is blocked.
3. RTSP URI target outside allowed CIDRs is blocked before MediaMTX provisioning.
4. Raw password is absent from all API response schemas.
5. Public probe strips embedded RTSP userinfo and query.
6. XML with entity expansion/DTD is rejected by defusedxml.
7. HTTP redirects are disabled for ONVIF service calls.
8. Response body is bounded by ONVIF_MAX_RESPONSE_BYTES.
9. Operation and connect timeouts are bounded.

## Scale tests

Phase 2A does not broadcast across 100K cameras.

Test:
- 100K logical capability rows/load separately from media.
- queue-based probe worker in later regional-controller phase.
- per-site concurrency limits.
- simulated unreachable subnet to ensure no retry storm.

## Current QA status

Code-level unit cases: prepared.
Real camera integration: PENDING — requires reachable ONVIF camera/test simulator in the execution environment.
Container integration: PENDING — requires Docker-capable runner.
Security design review: PASS WITH FOLLOW-UPS — API authentication/RBAC is a broader platform blocker before untrusted production exposure.

Boss must not describe Phase 2A as production-certified until real-device and container integration gates pass.
