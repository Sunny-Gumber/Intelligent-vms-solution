# Third-Party Components and Licence Obligations

> **Not legal advice.** This is an engineering inventory to give counsel a
> starting point. Confirm obligations before distributing binaries.

## Runtime components

| Component | Licence | How it is used | Obligation |
| --- | --- | --- | --- |
| **FFmpeg** | LGPL-2.1+ or GPL-2.0+ **depending on build flags** | Invoked as a **separate executable process** | See the note below — the most important item here |
| **MediaMTX** | MIT | Separate process, programmed over HTTP | Attribution |
| **PostgreSQL** | PostgreSQL License (BSD-style) | Separate service | Attribution |
| FastAPI / Starlette | MIT / BSD-3-Clause | Library | Attribution |
| Uvicorn | BSD-3-Clause | Library | Attribution |
| SQLAlchemy / Alembic | MIT | Library | Attribution |
| Pydantic | MIT | Library | Attribution |
| PyJWT | MIT | Library | Attribution |
| argon2-cffi | MIT | Library | Attribution |
| cryptography | Apache-2.0 / BSD-3-Clause | Library | Attribution |
| httpx | BSD-3-Clause | Library | Attribution |
| structlog | Apache-2.0 / MIT | Library | Attribution |
| psutil | BSD-3-Clause | Library | Attribution |
| prometheus-client | Apache-2.0 | Library | Attribution |
| React | MIT | Web client | Attribution |
| LibVLCSharp | LGPL-2.1+ | Windows client | **Dynamic linking required**; see below |
| VLC (libvlc) | LGPL-2.1+ | Windows client | Ship unmodified; allow replacement |

## The FFmpeg question

FFmpeg's licence depends entirely on how **your** binary was compiled:

- Built with default flags → **LGPL-2.1+**
- Built with `--enable-gpl` (includes x264, x265) → **GPL-2.0+**
- Built with `--enable-nonfree` → **not redistributable at all**

Intelligent VMS **invokes FFmpeg as a separate executable** and never links
its libraries. This is a deliberate architectural decision recorded in
ADR-0006, taken partly for fault isolation and partly to keep this licence
boundary clean.

**Before shipping:**
1. Determine exactly which FFmpeg build you distribute (or require the
   customer to install).
2. If GPL, understand the implications for the rest of your distribution.
3. Consider shipping an LGPL build, or requiring the operator to install
   FFmpeg themselves — the latter avoids distribution obligations entirely
   and is what the Ubuntu deployment guide does.

## Codec patents — separate from copyright

H.264 (AVC) and H.265 (HEVC) are covered by patent pools (MPEG LA / Via LA /
Access Advance). **A permissive software licence does not grant patent
rights.** Obligations typically attach to the party distributing an
encoder/decoder, and in a VMS the cameras do the encoding while the server
copies the stream.

This product's default path is **stream copy**, not transcoding, which is a
materially different posture from a product that re-encodes. Have counsel
confirm your specific situation.

## LibVLCSharp / VLC in the Windows client

LGPL-2.1+ requires that the user be able to replace the library. In practice:

- Link dynamically (the default for LibVLCSharp).
- Ship the VLC binaries unmodified, in a replaceable location.
- Include the LGPL text and a written offer for the VLC source.

Do not statically link or repackage libvlc without legal review.

## Generating the SBOM

```bash
python tools/generate_sbom.py --output release/sbom.json
```

Produces a CycloneDX-style inventory from the dependency manifests. The
release workflow attaches it to every tagged build.

## Before first commercial distribution

- [ ] Replace the placeholder `LICENSE` with the final licence
- [ ] Decide and document which FFmpeg build is distributed or required
- [ ] Obtain legal sign-off on codec patent exposure
- [ ] Verify LGPL compliance for the VLC components
- [ ] Generate and publish the SBOM with each release
- [ ] Confirm every component above still matches the installed versions
