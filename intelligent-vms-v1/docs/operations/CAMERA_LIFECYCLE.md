# Camera lifecycle management

This document covers logical camera metadata, groups, credential rotation and
physical camera replacement for Category 1 lifecycle management.

## Stable logical identity

The VMS treats a managed camera as a logical resource. These operations preserve:

- camera `id`;
- live `stream_key`;
- recording `record_stream_key`;
- recording policy and retained media;
- alarm, AI-policy, health and other camera-ID references.

Renaming a camera therefore changes only its display name. It does not rename the
MediaMTX paths or historical references.

Tenant and site are immutable through lifecycle APIs. Moving a camera between
sites is a separate migration/placement operation and is not performed by these
endpoints.

## Rename, location and group assignment

Update one or more logical metadata fields:

```http
PATCH /api/v1/cameras/{camera_id}
Content-Type: application/json

{
  "name": "Main Gate",
  "location_description": "Building A - vehicle entrance",
  "group_id": "camera-group-id"
}
```

`location_description` and `group_id` may be set to `null` to clear them.
A group assignment is accepted only when the group belongs to the exact same
tenant and site as the camera.

## Camera groups

Create a group:

```http
POST /api/v1/camera-groups
Content-Type: application/json

{
  "tenant_id": "default",
  "site_id": "site-noida-01",
  "name": "Entrances",
  "description": "Vehicle and pedestrian entrance cameras"
}
```

List visible groups with `GET /api/v1/camera-groups`.

Update a group with `PATCH /api/v1/camera-groups/{group_id}`.

Deleting a group with `DELETE /api/v1/camera-groups/{group_id}` unassigns
cameras in that same tenant/site but does not delete those cameras.

## Credential rotation

Rotate one or both credential fields:

```http
PATCH /api/v1/cameras/{camera_id}/credentials
Content-Type: application/json

{
  "username": "vms-service",
  "password": "new-camera-password"
}
```

Omitted fields keep their existing encrypted value. An explicit `null` clears
that credential field.

In single-node mode, the VMS applies the new live source and any enabled
continuous-recording source before committing the database change. If external
media reconfiguration or database commit fails, it performs a best-effort restore
of the prior source and returns a bounded error without credentials or source URLs.

In placement mode, the assignment owner and generation are preserved. The VMS
clears the assignment's applied-generation marker and sets the camera to
`pending-source-refresh`; the existing reconciler reapplies the same assignment
with the new source.

## Physical camera replacement

Replace the physical source while keeping the logical camera identity:

```http
POST /api/v1/cameras/{camera_id}/replace
Content-Type: application/json

{
  "host": "10.40.2.31",
  "rtsp_port": 554,
  "main_path": "/cam/realmonitor?channel=1&subtype=0",
  "sub_path": "/cam/realmonitor?channel=1&subtype=1",
  "username": "vms-service",
  "password": "replacement-password"
}
```

The replacement host and stream paths are validated against the exact
tenant/site camera CIDRs configured in `ONVIF_SITE_ALLOWED_CIDRS_JSON`. DNS
targets must resolve to exactly one site-approved address before downstream use.

Omitted replacement credential fields keep the previous encrypted value; explicit
`null` clears the supplied field.

A physical replacement invalidates the stored ONVIF capability snapshot because
firmware, services and profiles describe the old device. Run a fresh ONVIF probe
or onboarding/capability-refresh flow before using device-specific metadata for
the replacement.

## Qualification boundary

These APIs provide software lifecycle behavior only. They do not by themselves
prove interoperability with any named camera vendor/model/firmware and do not
change the current external-qualification or scale status.
