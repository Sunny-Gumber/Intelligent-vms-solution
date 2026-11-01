# Operator Guide

For day-to-day monitoring staff. Administration is covered separately in
`administrator-guide.md`.

## Signing in

Your administrator provides a username and password. If multi-factor
authentication is enabled you will also be asked for a six-digit code from
your authenticator app.

**A code can only be used once.** If you have just finished enrolling, wait
for the next code (up to 30 seconds) before signing in. This is deliberate: it
stops anyone replaying a code they observed.

## Live monitoring

Choose a layout (1x1, 2x2, 3x3, 4x4) and drag cameras from the tree onto
tiles. Your layout is saved to your account and follows you to any
workstation.

**Tiles are independent.** If one camera fails, that tile shows an error and
keeps retrying; every other tile keeps playing. You never need to reload the
whole wall because one camera is down.

The system picks the right stream quality automatically — a small tile gets
the sub stream, a large one gets the main stream. This is what keeps the
network usable with many tiles open. You can override it per tile.

### Tile status

| Shown | Meaning |
| --- | --- |
| Live | Playing normally |
| Connecting / Reconnecting | Establishing or retrying; it will keep trying |
| "Live video is unavailable (media gateway down)" | Live view is broken, **recording is not** |
| "You do not have access to this camera" | A permissions matter for your administrator |

## PTZ

Select a tile and use the PTZ panel. Three things to know:

1. **One operator at a time.** If someone else is controlling a camera you
   will be told who. A supervisor can take control.
2. **Movement stops automatically.** Every movement has a server-enforced time
   limit, so a camera never drifts if your connection drops mid-command.
3. **Presets only appear if the camera actually supports them.** The system
   does not show controls a device cannot perform.

## Playback

Pick a camera and a time range.

**Read the timeline carefully.** Solid bars are recorded footage; gaps are
drawn as gaps. If footage is missing for a period, the system says so
explicitly — it never draws a solid bar over a hole. Expand "gaps in this
period" to see exactly when and why.

If you ask for a period with no recording, you get a plain message saying so
and suggesting you check the schedule and retention policy.

## Exporting evidence

1. Select the camera and the exact time range.
2. Enter a reason, and a case reference if you have one. Both are stored.
3. Submit. Large exports run in the background with a progress indicator.
4. When complete, note the **SHA-256 hash** shown with the export.

### Why the hash matters

The hash proves the file has not been altered since the system produced it. If
the export is used in an investigation, record the hash alongside it. Anyone
can recompute it later and confirm the file is unchanged:

```
Windows:  certutil -hashfile export.mp4 SHA256
Linux:    sha256sum export.mp4
```

The system also records who requested the export, when, from which address,
and exactly which recording segments it used.

### If an export fails

If any part of the source footage is missing, **the export fails rather than
producing a shorter file.** This is intentional. An export that silently omits
three minutes looks complete and is worthless as evidence. Report the error to
your administrator; it names the missing segments.

## Alarms

Open alarms appear in the alarm list and update live.

- **Acknowledge** — you have seen it and are dealing with it
- **Resolve** — the situation is handled
- **Open footage** — jumps straight to the video at the time of the alarm

If two operators acknowledge the same alarm at the same instant, one wins and
the other is told who handled it. Nobody's action is silently overwritten.

## Snapshots

Captures a single frame from the live camera. Snapshots are hashed and
attributed to you, like exports.

## When to call your administrator

- A camera shows "unauthorized" (its password has changed)
- A camera is offline and you can see it is powered on
- Playback shows gaps you did not expect
- A storage warning appears
- Live view fails but you still need to review recordings (you can — use
  playback; recording is independent of live view)
