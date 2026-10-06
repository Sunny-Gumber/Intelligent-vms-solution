# RTSP and RTSPS camera sources — issue #27

Product status: **Release Candidate / External Qualification Pending**.
Acceptance, exact fixing PR/head/merge and CI are recorded in
[issue #27](https://github.com/Sunny-Gumber/Intelligent-vms-solution/issues/27).

## Configuration and compatibility

Camera create accepts `source_protocol: "rtsp" | "rtsps"`; omission defaults to
RTSP. Migration 0019 backfills existing rows as RTSP, keeps a non-null server
default and constrains allowed values. No historical migration changes.
The public representation exposes protocol and optional certificate fingerprint;
it never exposes username/password, encrypted credentials or source URIs.

Use separate host, port, MAIN/SUB/THIRD path and credential fields. Do not paste a
credential-bearing URL. Credentials stay encrypted at rest and are encoded only
in internal media source URIs. Credential query parameters and ambiguous URI
components are rejected. Site CIDRs, single-answer DNS/IP pinning, tenant/site
authorization and browser session/CSRF remain mandatory for both protocols.

The camera form offers **Replace existing source**, using the existing authorized
replacement API. Select the logical camera and re-enter its stream paths. It
preserves ID, stream keys, recording history and encrypted credentials by default;
uncheck **Keep encrypted credentials** only to explicitly replace/clear them.
Replacement API protocol omission preserves the current protocol; an RTSPS to
RTSP change must be explicit. Replacement resets certificate trust to the supplied
fingerprint or normal PKI verification; credential-only rotation preserves trust.
Source protocol/trust changes refresh recording and distributed assignments, and
failed source changes restore the previous protocol/trust along with credentials.

## Certificate trust and media runtime

Pinned upstream MediaMTX 1.21.1 supports RTSPS sources. Without a fingerprint,
Go's normal certificate chain and hostname/IP verification applies. A self-signed
certificate or a DNS-only certificate used through the pinned IP fails unless an
operator explicitly approves the exact device certificate.

Optional `source_fingerprint` is 64 hexadecimal characters: SHA-256 of the DER
leaf certificate. It requires RTSPS and is not a password. An approved fingerprint
uses MediaMTX exact leaf matching **instead of** chain/hostname/expiry checks.
It is not full PKI qualification, automatic trust or a global verification disable.
Obtain and verify the certificate fingerprint through a trusted device-management
channel. Do not auto-trust a fingerprint downloaded from an unauthenticated
network connection. Certificate rotation requires new explicit operator approval.
No RTSPS failure silently retries using RTSP. Source provisioning config acceptance
does not prove the camera is online; inspect camera health after activation.

Official upstream sources:
- [RTSPS source](https://github.com/bluenviron/mediamtx/blob/v1.21.1/internal/staticsources/rtsp/source.go)
- [Certificate semantics](https://github.com/bluenviron/mediamtx/blob/v1.21.1/internal/protocols/tls/make_config.go)
- [Redirect behavior](https://github.com/bluenviron/gortsplib/blob/v5.6.6/client.go)

Upstream follows RTSP redirects carrying camera credentials outside the initially
pinned address. VMS runtime **1.21.1-vms.1** applies one source response callback
patch deleting `Location` for 3xx responses before redirect handling/logging. It
blocks all camera source redirects, including same-site redirects. Cameras requiring
redirects must be configured using their final validated endpoint. This confines
RTSP and RTSPS sources equally without a proxy or transcoding.

Linux and Windows use the same checksum-pinned source and patch. The builder pins
Go 1.26.8, verifies generated HLS assets, and produces reproducible Windows ZIPs
with fixed timestamps and pinned ZIP/executable SHA-256. Windows installs remain
offline; upgrade/repair replaces an older media executable after services stop.
Linux Docker builds the patched runtime. Distributed nodes must deploy this VMS
media image/runtime too; an unpatched external MediaMTX binary does not satisfy
the source confinement contract. Helm uses operator-supplied immutable VMS images.

For a source build: install pinned Go 1.26.8 and Python 3.12, then use
`python tools/build_mediamtx.py --work <build-directory> --output <binary-or-windows-zip> --target-os linux|windows`.
Build manifest: `infra/mediamtx/runtime.json`. No camera secrets enter the build.

## Stream roles and supplied hardware evidence

MAIN stays authoritative for source-copy recording. SUB remains the normal live
default when present; THIRD is optional live only. MediaMTX does not transcode.

Operator-supplied FFprobe evidence for CP-UNC-VE21ZL4C-VMDS-Q:

| Source | Supplied external probe evidence |
|---|---|
| RTSPS MAIN `/0` | HEVC/H.265, 3840×2160, 20 FPS; PCM mu-law audio — PASS |
| RTSPS SUB `/1` | H.264, 1920×1080, 20 FPS; PCM mu-law audio — PASS |
| THIRD `/2` | HEVC detected, zero width/height and invalid reported rate — NOT QUALIFIED |

This is supplied standalone probe evidence, not VMS hardware qualification.
Do not configure THIRD for the first VMS test. Use SUB H.264 for browser live;
this milestone does not require HEVC browser playback or transcode MAIN.

## Existing Ubuntu VM retest — perform manually after acceptance

Use the exact accepted merge SHA from issue #27:

```sh
cd ~/Intelligent-vms-solution
git fetch origin
git checkout <accepted-merge-sha-from-issue-27>
cd intelligent-vms-v1
bash deploy/field-test/vmsctl.sh upgrade
bash deploy/field-test/vmsctl.sh health
python3 deploy/field-test/mint_access_token.py --ttl-seconds 600
```

Open `http://localhost:8080`, use a NEW temporary token, then add the camera (or
replace its existing logical source): tenant `field-test`, site `site-01`, name
`CP-UNC-VE21ZL4C-VMDS-Q`, protocol **RTSPS**, host `192.168.1.100`, port **554**,
MAIN `/0`, SUB `/1`, THIRD **blank**, username `admin`; enter the password privately.
If the certificate is self-signed, supply its independently approved SHA-256
fingerprint. The host must belong to the existing site's configured camera CIDRs;
do not broaden the policy to bypass a rejection.

Next manual checks: onboarding/online state, SUB H.264 live, MAIN recording and
continuity, playback, snapshot, clip export, manual recording, restart persistence,
host reboot recovery and diagnostic credential redaction. Record actual PASS /
FAIL / NOT TESTED only after execution. No real-camera, THIRD, HEVC-browser,
Windows 10/11, performance or production qualification is claimed by CI.

## Executable evidence

`tests/test_camera_source_protocol.py` covers creation, URI encoding/injection,
protocol rejection, target policy/scope, public/log secrecy, replacement/rotation,
rollback, all roles, distributed reconciliation/MAIN recording, trust clearing,
diagnostic redaction and 0018 upgrade/backfill/constraint/downgrade safety.

`tools/check_camera_transport_runtime.py --binary <actual-media-executable>` uses
ephemeral loopback RTSP/TLS fixtures against the actual packaged runtime. It checks
RTSP/RTSPS PLAY setup, approved leaf trust, rejection without trust/wrong trust,
no plaintext fallback, and zero redirected-target connections for RTSP and RTSPS.
No real camera, footage, tokens or persistent private keys are used.

Ubuntu 22.04/24.04 Chromium smoke additionally checks RTSP default, RTSPS form
payload, explicit protocol replacement preserving identity/credentials, invalid
API protocol rejection and existing login/session/CSRF boundaries. Existing
persistence, recording, snapshot, playback, export and security gates remain.

After merged post-merge CI acceptance, STOP engineering work before hardware claims.
