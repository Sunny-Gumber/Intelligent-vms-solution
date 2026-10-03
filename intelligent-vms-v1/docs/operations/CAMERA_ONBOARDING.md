# Camera onboarding

## ONVIF path

1. Optional LAN discovery:
```bash
curl -X POST http://localhost:8000/api/v1/onvif/discover \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"default","site_id":"site-noida-01","timeout_seconds":2.5}'
```

2. Probe a known camera:
```bash
curl -X POST http://localhost:8000/api/v1/onvif/probe \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id":"default",
    "site_id":"site-noida-01",
    "host":"192.168.1.101",
    "port":80,
    "scheme":"http",
    "username":"admin",
    "password":"camera-password"
  }'
```

Discovery and probe requests are tenant/site scoped and are rejected before network access when the caller is outside that scope. The response contains device identity, observed service capabilities, normalized media profiles, and recommended main/sub profile tokens. Passwords, query tokens, raw SOAP fault text and credential-bearing service/stream URIs are never returned.

3. Onboard:
```bash
curl -X POST http://localhost:8000/api/v1/onvif/onboard \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id":"default",
    "site_id":"site-noida-01",
    "name":"Gate 01",
    "host":"192.168.1.101",
    "port":80,
    "username":"admin",
    "password":"camera-password"
  }'
```

To override defaults, provide `main_profile_token` and/or `sub_profile_token`.

4. View capability snapshot:
`GET /api/v1/onvif/cameras/{camera_id}/capabilities`

5. Refresh:
`POST /api/v1/onvif/cameras/{camera_id}/refresh`

## Manual RTSP fallback

If ONVIF is disabled/non-conformant, use the existing:
`POST /api/v1/cameras`

with host, main/sub path and credentials. Manual targets are validated against the exact `tenant/site` camera CIDRs in `ONVIF_SITE_ALLOWED_CIDRS_JSON` before persistence. DNS names must resolve to exactly one site-approved address; the VMS stores and provisions the validated IP so MediaMTX cannot re-resolve the name to a different destination after validation.

## Network policy

The global fallback policy is configured with `ONVIF_ALLOWED_CIDRS`, but tenant/site-scoped ONVIF probe, onboarding and refresh require an exact entry in `ONVIF_SITE_ALLOWED_CIDRS_JSON`. Example:

```text
ONVIF_SITE_ALLOWED_CIDRS_JSON={"default/site-noida-01":["10.40.0.0/16","192.168.10.0/24"]}
```

Camera-advertised Device/Media service URLs and RTSP stream URIs are rechecked against that same site policy. DNS targets must resolve to exactly one approved address before a downstream connection is made. Credential-bearing ONVIF service URLs are rejected.

Do not set `ONVIF_ALLOW_PUBLIC_HOSTS=true` unless the deployment explicitly requires public camera addresses and compensating egress controls exist. This global switch does not override the exact per-site CIDR policy for scoped ONVIF operations.

## Discovery and Docker

LAN multicast discovery may not work from Docker bridge networking. Manual-IP probe/onboarding is the authoritative functional path in that topology. Production discovery workers must execute at the site/region close to camera networks. Configure `ONVIF_DISCOVERY_LOCAL_SITES` with exact `tenant/site` keys that are physically reachable from that process; discovery requests for any other site fail before opening the multicast socket. A wildcard is supported only when deliberately configured for a genuinely shared local discovery domain.

## Current Phase 2A limitations

- Media v1 is the implemented stream-discovery path; Media2-specific fallback is not complete yet.
- No ONVIF event subscription yet.
- No PTZ control yet.
- No edge recording/replay yet.
- No recording/playback service yet.
- Camera credentials use application-level encryption in this prototype; production moves them to Vault/KMS.


## Camera deletion and retained recordings

In single-node mode, deleting a camera removes an enabled continuous-recording
path before removing the live path and database row. Cleanup failure is fail-closed:
the camera row is retained and the API returns a bounded error without upstream
details. Disabled/non-continuous policies do not have an active recorder path to
remove.

Deleting the MediaMTX path stops future recording. Already written segment files
remain subject to the configured storage/retention lifecycle; camera deletion does
not securely erase retained media files. Database metadata tied by foreign key to
the camera is removed according to the schema's cascade rules.
