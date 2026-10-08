# ONVIF camera configuration controls

This runbook covers the Track A1 software implementation for managed ONVIF
camera configuration. These controls are capability-dependent and remain
`verification_status=NV` until named camera model/firmware evidence exists.

## Security boundary

Every device/service request is authorized through the managed camera's
tenant/site scope. Advertised ONVIF service endpoints are revalidated and pinned
against the exact site camera network policy before use. Stored camera
credentials are decrypted server-side only for the device request and are never
returned to API clients.

Public errors are bounded. Camera SOAP fault text, credential-bearing URLs and
blocked target details are not exposed. Expected HTTP errors pass through
unchanged, including an invalid managed-stream role (422) and a missing
capability snapshot or stale profile (404 or 409). Device and transport
failures stay HTTP 502 with the generic message "ONVIF operation failed".

## Managed stream roles

The VMS stores main and sub ONVIF profile selections during onboarding. An
optional third role can be selected from the current capability snapshot:

```http
PUT /api/v1/onvif/cameras/{camera_id}/profiles/third

{"profile_token":"profile-token"}
```

Main, sub and third roles reuse the camera's ONVIF profile/configuration tokens.
A capability refresh clears the stored third role if that token no longer exists.

## Encoder configuration

Read current state plus advertised writable options:

```http
GET /api/v1/onvif/cameras/{camera_id}/encoder/main
```

Write selected standard encoder fields:

```http
PUT /api/v1/onvif/cameras/{camera_id}/encoder/main

{
  "width": 1920,
  "height": 1080,
  "fps": 25,
  "bitrate_kbps": 4096,
  "encoding_interval": 1,
  "gov_length": 50,
  "quality": 6,
  "bitrate_mode": "CBR"
}
```

The VMS reads `GetVideoEncoderConfigurationOptions`, rejects values outside the
advertised ranges/resolutions, applies the configuration and then reads it back.

Codec information is exposed from the selected profile, but generic cross-codec
switching remains fail-closed until a device-qualified Media2 path is available.
Therefore the broader H.264/H.265/MJPEG checklist row remains TARGET.

## Imaging configuration

Read:

```http
GET /api/v1/onvif/cameras/{camera_id}/imaging/main
```

Write supported fields:

```http
PUT /api/v1/onvif/cameras/{camera_id}/imaging/main

{
  "brightness": 50,
  "contrast": 50,
  "saturation": 50,
  "sharpness": 40,
  "ir_cut_filter": "AUTO",
  "white_balance_mode": "AUTO",
  "backlight_mode": "OFF",
  "wdr_mode": "ON",
  "wdr_level": 50,
  "exposure_mode": "AUTO"
}
```

The VMS reads Imaging `GetOptions` and validates requested numeric ranges/modes
before `SetImagingSettings`, followed by readback. Hue and anti-flicker are used
only when a recognized field is actually advertised by the device.

The standard `IrCutFilter` control implements day/night switching where the
camera advertises it.

## IR illuminator

```http
GET /api/v1/onvif/cameras/{camera_id}/ir
PUT /api/v1/onvif/cameras/{camera_id}/ir

{"mode":"Auto"}
```

The VMS sends only the reserved standard auxiliary commands
`tt:IRLamp|On`, `tt:IRLamp|Off` or `tt:IRLamp|Auto`, and only if the camera
advertises the exact command. Vendor-private illuminator commands are not
invented or inferred.

## Rotation and mirror

```http
GET /api/v1/onvif/cameras/{camera_id}/orientation/main
PUT /api/v1/onvif/cameras/{camera_id}/orientation/main

{"rotation_mode":"ON","rotation_degree":90,"mirror":true}
```

Rotation and mirror are written only when their source-configuration options
advertise the operation. Generic vertical flip is deliberately unsupported in
this adapter; the combined rotation/mirror/flip checklist row therefore remains
TARGET.

## Video-source modes and PAL/NTSC

Raw advertised source modes:

```http
GET /api/v1/onvif/cameras/{camera_id}/video-source-modes/main
PUT /api/v1/onvif/cameras/{camera_id}/video-source-modes/main

{"mode_token":"advertised-token"}
```

Conservative PAL/NTSC mapping:

```http
GET /api/v1/onvif/cameras/{camera_id}/video-standard/main
PUT /api/v1/onvif/cameras/{camera_id}/video-standard/main

{"standard":"PAL"}
```

PAL/NTSC is accepted only when exactly one advertised VideoSourceMode
description explicitly identifies the requested standard. Ambiguous or generic
modes fail as unsupported rather than being guessed.

## Date/time

```http
GET /api/v1/onvif/cameras/{camera_id}/date-time
PUT /api/v1/onvif/cameras/{camera_id}/date-time

{
  "mode":"Manual",
  "daylight_savings":false,
  "timezone":"UTC0",
  "utc_datetime":"2026-09-28T07:30:00Z"
}
```

Manual time requires a timezone-aware datetime. Writes use Device Management
`SetSystemDateAndTime` and are followed by `GetSystemDateAndTime` readback.

## Text OSD and camera-name overlay

List/create/update/delete OSDs:

```text
GET    /api/v1/onvif/cameras/{camera_id}/osds
POST   /api/v1/onvif/cameras/{camera_id}/osds?role=main
PATCH  /api/v1/onvif/cameras/{camera_id}/osds/{osd_token}
DELETE /api/v1/onvif/cameras/{camera_id}/osds/{osd_token}
```

Create checks `GetOSDOptions` before mutation, including text support and
advertised maximum OSD count. Existing non-Plain Date/Time OSD content is not
silently converted to Plain text.

Camera-name convenience endpoint:

```http
PUT /api/v1/onvif/cameras/{camera_id}/osds/camera-name?role=main
```

With no `osd_token`, it creates a Plain OSD using the managed camera name.
With an existing Plain OSD token, it updates that OSD to the managed camera name.

## Privacy masks

Media2 privacy masks use the selected profile's video source configuration:

```text
GET    /api/v1/onvif/cameras/{camera_id}/privacy-masks/main
POST   /api/v1/onvif/cameras/{camera_id}/privacy-masks/main
PATCH  /api/v1/onvif/cameras/{camera_id}/privacy-masks/main/{mask_token}
DELETE /api/v1/onvif/cameras/{camera_id}/privacy-masks/main/{mask_token}
```

Create/update reads `GetMaskOptions` first and validates polygon point count,
rectangle-only constraints and advertised mask type before mutation, then reads
the list back.

## Qualification boundary

These endpoints establish software protocol handling and deterministic mocked
SOAP behavior. They do not establish interoperability with a named vendor,
camera model or firmware. Unsupported or partially standardized operations fail
closed rather than returning synthetic success.
