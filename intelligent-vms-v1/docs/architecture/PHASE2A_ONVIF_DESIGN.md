# Phase 2A ONVIF design

Status: Architect Agent design approved for implementation.

## Boundary

Phase 2A adds a camera-onboarding subsystem. It does not yet implement event subscriptions, recording/playback, PTZ control, edge-recording retrieval or AI inference.

## Components

### Control API
Owns tenants/sites/camera records and public onboarding APIs.

### ONVIF adapter
A protocol module with:
- WS-Discovery client
- safe target validation
- SOAP client
- Device service probe
- Media profile enumeration
- stream URI retrieval
- normalized capability output

In Phase 2A it is packaged with the control-api for deployability. The interface is intentionally stateless so it can be extracted into per-site discovery workers later without changing API contracts.

### Media provisioner
Existing MediaMTX adapter receives the selected RTSP source. It never becomes the camera registry.

## Public APIs

### POST /api/v1/onvif/discover
Runs a bounded local WS-Discovery scan.
Request: timeout_seconds (bounded)
Response: list of EPR/XAddr/scopes/type hints.

### POST /api/v1/onvif/probe
Inputs: host, port, username, password, optional device_service_path.
Returns:
- device_info
- observed services/capabilities
- normalized media profiles
- recommended main/sub tokens

No password or credential-bearing URI is returned.

### POST /api/v1/onvif/onboard
Inputs:
- tenant/site/name
- camera connection/auth fields
- optional explicit main/sub profile tokens

Actions:
1. validate target network policy;
2. probe;
3. reject duplicate tenant+site+host;
4. select requested/default profiles;
5. fetch stream URIs;
6. create camera;
7. encrypt credentials;
8. save capability snapshot;
9. provision sub stream if available, otherwise main stream, to MediaMTX;
10. commit only after provision succeeds.

### POST /api/v1/onvif/cameras/{camera_id}/refresh
Re-probes and stores a new capability snapshot. Media reassignment is not implicit unless selected stream identity changed.

## Data model

New camera_capabilities table:
- id
- camera_id
- onvif_xaddr
- device_info_json
- services_json
- features_json
- profiles_json
- main_profile_token
- sub_profile_token
- probed_at
- probe_version

This is separated from cameras so Phase 2A does not require destructive modification of the existing camera table.

## Error model

Errors map to stable codes:
- TARGET_NOT_ALLOWED
- NETWORK_UNREACHABLE
- AUTH_FAILED
- SOAP_FAULT
- DEVICE_SERVICE_INVALID
- MEDIA_SERVICE_UNAVAILABLE
- NO_MEDIA_PROFILES
- NO_STREAM_URI
- DUPLICATE_CAMERA
- MEDIA_PROVISION_FAILED

Operator-facing detail may include host/error class but never username/password or credential-bearing URI.

## Retry policy

Interactive onboarding:
- connect timeout <= 3 s
- operation timeout <= 8 s
- at most one compatibility retry per SOAP operation
- no unbounded retry inside HTTP request

Background refresh:
- exponential retry belongs in a job worker later, not API request threads.

## Scale design

100K+ cameras are partitioned by site/region. Discovery is local to site networks. Probe work becomes queue-driven with a configurable concurrency budget per site/subnet.

The central control plane stores results and desired state. It does not multicast into every remote network.

## Duplicate policy

Phase 2A rejects same tenant + site + resolved host. Future identity reconciliation can additionally use serial/device UUID/MAC when safely available.

## SSRF/network policy

Default allowed ranges are private/link-local/loopback ranges configurable with ONVIF_ALLOWED_CIDRS. ONVIF_ALLOW_PUBLIC_HOSTS=false by default.

Every device-service XAddr and media service XAddr is revalidated before use. Redirect following is disabled.

## XML policy

- defusedxml parser
- maximum response bytes
- no external entity resolution
- namespace-local-name matching to tolerate vendor prefix differences

## Boss acceptance

Phase 2A passes only if manual probe/onboard works against a standards-conforming test fixture, discovery parser tests pass, secrets are redacted, invalid/public targets are blocked by default, and existing manual RTSP onboarding remains available.
