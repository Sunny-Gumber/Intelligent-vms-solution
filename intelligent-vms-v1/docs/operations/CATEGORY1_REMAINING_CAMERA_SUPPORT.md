# Remaining Category 1 camera-support routes

This runbook covers the final generic Track A1 software routes implemented after
the ONVIF configuration milestone. It does not establish named camera/vendor
interoperability. All device-dependent rows remain `verification_status=NV`
until external qualification exists.

## Addition by serial number

```http
POST /api/v1/onvif/onboard/serial
```

The serial route is intentionally site-local. It:

- requires authorized tenant/site scope;
- requires the configured site camera network policy and local WS-Discovery site;
- keeps at most 32 site-approved discovered ONVIF device-service endpoints;
- uses a lightweight `GetDeviceInformation` identity request rather than a full
  media probe for every candidate;
- limits concurrent identity requests to four and applies a capped overall
  deadline;
- requires an exact ONVIF `SerialNumber` match;
- rechecks that exact serial during the full onboarding probe before persistence;
- rejects duplicate serial claims;
- sends only the matched endpoint through the normal secure ONVIF onboarding
  workflow.

This is not a vendor-cloud or internet serial-number lookup.

## QR onboarding boundary

```http
POST /api/v1/onvif/onboard/qr
```

The backend accepts versioned decoded QR text:

```text
vms-onvif:v1:<base64url-json>
```

The JSON can contain only bounded connection/scope/profile fields. Usernames,
passwords, tokens and secrets are not allowed in the QR payload. Credentials are
supplied separately to the authenticated API request.
The device-service path must not contain query/fragment credentials. Malformed
base64url payloads and decorated ONVIF service addresses fail closed.

The current development web UI does not contain a QR-camera scanner. Therefore
the complete market feature "Addition by QR code" remains TARGET even though the
credential-safe backend onboarding contract is implemented.

## Third-stream VMS media path

A managed ONVIF profile can be assigned to main, sub or third:

```http
PUT /api/v1/onvif/cameras/{camera_id}/profiles/third

{"profile_token":"profile-third"}
```

The third role creates a stable derived MediaMTX path and public WebRTC/HLS URLs.
It shares the camera's existing media-node placement rather than introducing a
new scheduler role.

The third path participates in:

- single-node provisioning and rollback;
- credential rotation and physical replacement;
- distributed reconciliation and stale-path cleanup;
- camera deletion;
- media-node handoff cleanup;
- node fencing/revocation through a backward-compatible multi-key fence payload.

Upgrade every node agent to the version that consumes the multi-key fence
payload before activating third streams. Older agents acknowledge only the
legacy live key and cannot fence an active third path. Do not downgrade a node
agent while third paths remain active; remove and verify cleanup of those paths
first.

Configure at least two media reconciliation changes per run when using a third
stream; a lower budget leaves multi-path refresh and stale-owner cleanup pending
to avoid accepting a partially refreshed source. The retained third key is
cleaned even after the profile is cleared. Refreshing device capabilities
disables an active third stream if its profile disappears; continuous recording
remains on the independent main-stream recording path.
If a first-time third path was added but its database commit and immediate
cleanup both failed, the single-node reconciler retries its deterministic
derived key while the camera remains enabled. Operators should inspect
MediaMTX paths before disabling or removing a camera after such a failure.

Disable the third role with:

```http
DELETE /api/v1/onvif/cameras/{camera_id}/profiles/third
```

The stable third stream key can be retained while the active path is removed.

## H.264 / H.265 / MJPEG profile support

The VMS does not force an unsafe encoder family change. Instead it can select an
existing advertised ONVIF profile for a managed role:

```http
PUT /api/v1/onvif/cameras/{camera_id}/profiles/{role}/codec

{"encoding":"H265"}
```

Accepted API codec names are `H264`, `H265`, and `MJPEG`. ONVIF `JPEG`
profiles are treated as the MJPEG/JPEG media-profile family for selection.

The route re-probes the camera, chooses an unused matching profile (or validates
an explicitly preferred profile), requires the same camera RTSP endpoint, then
re-provisions the managed media role using the existing source lifecycle.

This establishes software support for consuming advertised codec profiles. It
does not claim that every named camera supports every codec.

## Rotation, mirror and vertical flip

ONVIF source configuration already supports advertised rotation and mirror
operations. Vertical flip is implemented only when the camera advertises the
standard primitives required to represent it:

- Mirror control;
- rotation mode `ON`;
- rotation degree 180.

When all three are advertised, vertical flip is represented as mirror plus 180°
rotation. If those primitives are absent, the request fails as unsupported rather
than issuing vendor-private commands.

## Manufacturer-specific drivers

A deterministic manufacturer-driver registry/contract now exists so future
adapters can be installed without hard-coding vendor behavior into generic ONVIF
services. No actual vendor driver is registered by this milestone.

Consequently F01-011 remains TARGET. It must not move to QA until at least one
real manufacturer adapter has implemented a meaningful vendor-specific route,
with named model/firmware evidence kept separate from software CI.

## Qualification boundary

Mocked protocol tests, migrations and software integration evidence can promote
generic software rows to QA but cannot make them VERIFIED. Named vendor/model/
firmware, real-camera, site-network, hardware and scale evidence remain external
qualification work.
