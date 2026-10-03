# ONVIF troubleshooting

## Discovery returns no devices
Discovery is multicast and normally remains inside one broadcast domain. The requested `tenant/site` must have an exact `ONVIF_SITE_ALLOWED_CIDRS_JSON` entry and must be listed in `ONVIF_DISCOVERY_LOCAL_SITES` for the process handling discovery. Try manual IP probe first. Verify firewall/VLAN/container multicast behavior before changing VMS code.

## TARGET_NOT_ALLOWED
The management XAddr or RTSP URI resolved outside the requested site's configured camera networks, the site has no explicit CIDR policy, discovery is not local to that site, or a credential-bearing ONVIF service URL was rejected. Fix the exact `ONVIF_SITE_ALLOWED_CIDRS_JSON` / `ONVIF_DISCOVERY_LOCAL_SITES` entry; do not broadly disable the global network policy as the first fix.

## AUTH_FAILED
Verify ONVIF is enabled on the camera and that the account has ONVIF/API permission. Some cameras use a dedicated ONVIF user.

## DEVICE_SERVICE_INVALID
The endpoint did not return valid bounded ONVIF XML. Confirm device service path/port and inspect vendor interoperability separately.

## MEDIA_SERVICE_UNAVAILABLE
Device management works but Media v1 was not advertised. Check whether the device exposes Media2 only or has a vendor-specific implementation. Manual RTSP remains available while a tested adapter is developed.

## NO_MEDIA_PROFILES / NO_STREAM_URI
The camera did not return a usable profile or RTSP URI. Verify streaming is enabled and compare with the vendor's ONVIF conformance claim.

## MediaMTX provisioning failure
The database transaction is rolled back. Confirm the media node API is reachable from the control API and then retry onboarding.

## Browser cannot display H.265 main stream
Prefer an H.264 substream for browser live view while retaining H.265 main stream for recording. Browser codec support and WebRTC negotiation vary by client.
