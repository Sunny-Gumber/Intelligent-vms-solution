# Intelligent VMS web client

**Build status: this client has never been compiled.** No Node toolchain was
available in the environment that produced this source tree. `npm run build`
is Gate 4 of `docs/verification/external-qualification-plan.md`, and every
Module 14 requirement is marked "Partial — source delivered, not built" in the
feature matrix.

## Running

```bash
npm install
npm run dev          # expects the control API on http://localhost:8080
```

## Design notes

**Browsers cannot play RTSP.** `components/VideoTile.tsx` uses WebRTC/WHEP
against the media gateway, with HLS as a fallback. The client never receives a
camera URL or camera credentials — it receives a gateway path and a short-lived
play token (see `docs/architecture/security.md`).

**Permissions shown by this UI are cosmetic.** Every action is re-authorized on
the server. Hiding a button is a usability feature, not a security control.

**Tokens** are held in memory, not localStorage, to limit XSS exposure. The
refresh token is used once per rotation; reuse of a rotated token revokes the
whole session family server-side.
