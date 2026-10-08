# VMS-FIX-033 race reproduction report

Issue: [#106](https://github.com/Sunny-Gumber/Intelligent-vms-solution/issues/106).

This pass reproduces the seven conditional races. It does not patch them. Every reproduced test asserts the correct outcome, fails on this tree, and is marked `known_race`. The marker is deselected from the default suite in `tests/conftest.py`. Nothing is xfailed.

Reproductions ran on a disposable local PostgreSQL 16.15 database (`vms_fix_033` on `127.0.0.1`) after `alembic upgrade head` (revision `0018`). Session isolation, read from `SHOW transaction_isolation`, was **read committed** for every PostgreSQL case. The ONVIF case drives `services/onvif-event-worker/main.py` and the real `unsubscribe` function; it does not use a database.

The cited production files are unchanged between the start commit `28fd67f` and `origin/main` at the time of the run.

## How to run

Default suite (known races are deselected, not skipped):

```bash
cd intelligent-vms-v1
pip install -r tests/requirements.txt
VMS_SECRET_KEY=ci-test-key python3 -m pytest -q tests
```

Reproduced races. These fail until the follow-up fixes land. Point the URL at a disposable database; the tests migrate it and truncate application tables.

```bash
cd intelligent-vms-v1
VMS_SECRET_KEY=ci-test-key \
VMS_TEST_POSTGRES_URL='postgresql+asyncpg://USER:PASSWORD@127.0.0.1:5432/DATABASE' \
  python3 -m pytest -m known_race -q tests/test_vms_fix_033_race_reproductions.py
```

`VMS_ALARM_TRANSACTION_TEST_DATABASE_URL` is accepted as the existing equivalent. With neither variable set, each PostgreSQL reproduction skips:

`PostgreSQL race reproductions need VMS_TEST_POSTGRES_URL or VMS_ALARM_TRANSACTION_TEST_DATABASE_URL`

## Results

| # | Item | Classification | Attempts | Isolation | Observed wrong state | Follow-up |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Local-event duplicate insert | REPRODUCED | 20/20 | read committed | One row stored. The other caller raises `UniqueViolation` on `event_history_pkey` instead of returning `False`. | `VMS-FIX-037-candidate` |
| 2 | ONVIF task death after unexpected unsubscribe | REPRODUCED | 20/20 | n/a (worker task) | Camera task ends with `RuntimeError`. `pull` stays 1, `create` stays 1, and the supervisor does not replace it. | `VMS-FIX-038-candidate` |
| 3 | Alarm ACK versus close | REPRODUCED | 20/20 | read committed | Close commits `closed`. The stale acknowledge then commits `acknowledged` (HTTP 200). Final state is `acknowledged`. | `VMS-FIX-039-candidate` |
| 4 | Manual list-expiry versus an earlier stop | REPRODUCED | 20/20 | read committed | Explicit stop stores `stopped_at=12:00:40Z`. List expiry then stores `12:01:40Z` (`max_stop_at`). | `VMS-FIX-040-candidate` |
| 5 | Out-of-order heartbeat | REPRODUCED | 20 sequential and 20 concurrent, 40/40 | read committed | Older `observed_at` wins: `heartbeat_at=12:00:05Z`, `load.cpu=9`, `authority_mode=regional_autonomous`. | `VMS-FIX-041-candidate` |
| 6 | Recording-health monotonic write | REPRODUCED | 20/20 | read committed | Newer completion `seg-later` at `12:00:20Z` is overwritten by `seg-earlier` at `12:00:10Z`. | `VMS-FIX-042-candidate` |
| 7 | Camera delete versus an active manual recording | REPRODUCED | 20/20 | read committed | Camera row is gone. The manual session stays `ACTIVE` with `camera_id` NULL and `stopped_at` NULL. | `VMS-FIX-043-candidate` |

A control, not a race, is in the default suite: `test_onvif_known_unsubscribe_error_retries`. The real `unsubscribe` swallows `OnvifError`. Across 3/3 attempts the camera task stayed alive and pulled again (`pull=2`, `create=2`). That test passed.

## Commands and counts

Default suite, without a Postgres URL:

`1234 passed, 7 deselected, 2 warnings in 41.16s`

The 7 deselected tests are the `known_race` reproductions. The two warnings are pre-existing HMAC key-length warnings in `tests/test_auth_phase2b.py`.

Postgres plus ONVIF reproductions (`VMS_TEST_POSTGRES_URL` set, `-m known_race`):

`7 failed, 1 deselected in 31.42s`

The deselected test is the passing ONVIF `OnvifError` control, which does not carry `known_race`. Exit code 1 is the reproduced failures. Skip check without either URL, on `test_alarm_close_is_not_overwritten_by_stale_acknowledge`: `1 skipped`, exit 0, with the reason above.

`ruff check services tools tests` passed. `python3 tools/check_public_docstrings.py` passed.

## Per-item evidence

### 1. Local-event duplicate insert — REPRODUCED

Path: `persist_local_event_once` loads by primary key and, on a miss, `session.add`s the row (`services/control-api/app/services/local_event_store.py:11-25`). Callers commit afterwards (`services/control-api/app/routers/events.py:121-124`). The health monitor calls the same helper inside its own transaction (`services/control-api/app/services/health_monitor.py:507-508`). `event_history.event_id` is the primary key (`migrations/versions/0018_local_event_history.py:37`).

Method: two sessions call the real helper with one `event_id`. The first `AsyncSession.get` waits before insert. The second get also misses, inserts, and commits. The first then inserts and commits. 20/20.

Wrong state: `count=1`. The winner returns `True`. The loser raises `IntegrityError` / `UniqueViolationError` on `event_history_pkey` (`Key (event_id)=(evt-0) already exists`). It does not return `False`. The primary key stops a second row and aborts the loser's transaction, so a duplicate retry is not idempotent. The outbox path already avoids this with `ON CONFLICT DO NOTHING` (`services/control-api/app/services/outbox.py:164-196`).

`VMS-FIX-037-candidate`: insert the local event with `INSERT ... ON CONFLICT DO NOTHING` on `event_id` and return `False` when `rowcount` is 0, the same shape as `enqueue_message_once`. That keeps the surrounding transaction usable, including the health-monitor write that calls this helper.

### 2. ONVIF task death after an unexpected unsubscribe — REPRODUCED

Path: `camera_loop` catches a pull failure, then its `finally` always calls `unsubscribe` (`services/onvif-event-worker/main.py:273-288`). `unsubscribe` catches only `OnvifError` and otherwise propagates (`services/control-api/app/services/onvif_events.py:306-326`). The supervisor records the task in a dict and starts a new one only when the camera id is absent (`services/onvif-event-worker/main.py:312-324`). A finished task stays in that dict.

Method: the real `supervisor` and real `unsubscribe`. `pull_messages` raises `ConnectionError` once. `_soap` inside `unsubscribe` raises `RuntimeError`. Refresh is 0.01s. After unsubscribe, the test waits 0.25s (many supervisor iterations). 20/20.

Wrong state: one camera task, `live_camera_tasks=0`, `task_error='RuntimeError: synthetic unexpected unsubscribe failure'`, `pull=1`, `unsub=1`, `create=1`. No replacement task is created.

Control, 3/3, default suite: `_soap` raises `OnvifError`. The real handler logs and returns. The loop reconnects (`pull=2`, `create=2`) and the camera task stays live.

`VMS-FIX-038-candidate`: catch unexpected unsubscribe failures inside the `camera_loop` `finally`, log them, and continue the reconnect loop. In the supervisor, drop a task that is already done and start a replacement for that camera. Known `OnvifError` cleanup can stay best-effort.

### 3. Alarm ACK versus close — REPRODUCED

Path: acknowledge loads the row, rejects `state == "closed"`, then writes `acknowledged` and commits (`services/control-api/app/routers/alarms.py:494-506`). Close loads the same row, sets `closed`, and commits (`services/control-api/app/routers/alarms.py:528-534`). Neither statement uses `FOR UPDATE` or a compare-and-set predicate. VMS-FIX-002's rule-write fencing is a different path and does not cover these two handlers.

Method: acknowledge's `session.get` waits after it has loaded `open`. Close then runs to commit. Acknowledge resumes on its stale in-memory `open` row and commits. 20/20 at read committed.

Wrong state: close returns HTTP 200 with `state=closed`. Acknowledge then returns HTTP 200 with `state=acknowledged`. The stored final state is `acknowledged`.

`VMS-FIX-039-candidate`: serialize both handlers with `SELECT ... FOR UPDATE`, and make acknowledge a conditional update `SET state='acknowledged' WHERE id=:id AND state='open'`. If that updates zero rows, re-read and return HTTP 409 when the alarm is already closed. Close remains the terminal write.

### 4. Manual list-expiry versus an earlier stop — REPRODUCED

Path: list loads `ACTIVE` rows without a lock, then `_finalize_if_expired` sets `STOPPED` and `stopped_at=max_stop_at` when `max_stop_at <= now` (`services/control-api/app/routers/manual_recordings.py:45-51` and `:133-140`). Stop locks the row and sets `stopped_at=min(now, max_stop_at)` (`services/control-api/app/routers/manual_recordings.py:191-197`).

Method: the list `SELECT` waits before it reads the clock. Stop commits `stopped_at=2026-10-08T12:00:40Z` (40 seconds after start, before `max_stop_at`). The list clock then returns `max_stop_at` (`12:01:40Z`) and finalizes the stale `ACTIVE` object. 20/20.

Wrong state: a read between the two commits shows `STOPPED` at `12:00:40Z`. After the list commits, `stopped_at` is `12:01:40Z`. The earlier stop is lengthened to the cap.

`VMS-FIX-040-candidate`: lock the session with `FOR UPDATE` before expiry, re-read `state` under that lock, and write `stopped_at` only while the locked row is still `ACTIVE`. An already `STOPPED` row keeps the earlier `stopped_at`.

### 5. Out-of-order heartbeat — REPRODUCED

Path: `heartbeat_node` assigns `load_json`, `authority_mode`, and `heartbeat_at = min(observed_at, now)` from the payload alone (`services/control-api/app/routers/placement.py:124-134`). It does not compare `observed_at` with the stored `heartbeat_at`. The clamp stops a future clock from looking fresher than wall time. It does not stop an older observation from overwriting a newer one.

Method: base `heartbeat_at` is `12:00:00Z`. The newer payload is `observed_at=12:00:20Z`, `cpu=1`, `central_online`. The older payload is `observed_at=12:00:05Z`, `cpu=9`, `regional_autonomous`. Sequential: newer commits, then older runs to completion. Concurrent: the older transaction pauses on its commit, the newer transaction commits, then the older commit finishes. 20 sequential and 20 concurrent, all at read committed.

Wrong state, both schedules: stored `heartbeat_at=2026-10-08T12:00:05Z`, `load={'cpu': 9.0}`, `authority_mode='regional_autonomous'`.

`VMS-FIX-041-candidate`: update the node with a single conditional statement whose `WHERE` requires the clamped `observed_at` to be strictly newer than `heartbeat_at`. Leave `load_json`, `authority_mode`, and `heartbeat_at` unchanged when the incoming observation is older or equal. Return the stored row either way.

### 6. Recording-health monotonic write — REPRODUCED

Path: `record_segment_completion` loads the health row, and if `completed_at` is earlier than the in-memory `last_segment_completed_at` it returns without writing (`services/control-api/app/services/recording_health.py:126-136`). Otherwise it assigns the new segment fields (`:138-146`). The check is not a locked read and not a conditional `UPDATE`.

Method: the row starts at `seg-base` / `12:00:00Z`. The earlier completion (`seg-earlier`, `12:00:10Z`) waits after `session.get`. The later completion (`seg-later`, `12:00:20Z`) reads the same base, writes, and commits. The earlier completion then writes and commits. Both timestamps are after the base, so both pass the in-memory check. 20/20.

Wrong state: `last_segment_id='seg-earlier'`, `last_segment_completed_at=2026-10-08T12:00:10Z`.

`VMS-FIX-042-candidate`: perform the advance as `UPDATE recording_health_state SET ... WHERE camera_id=:id AND (last_segment_completed_at IS NULL OR last_segment_completed_at < :completed_at)`. If the predicate matches nothing, leave the newer row in place. Pair it with `SELECT ... FOR UPDATE` when the row must be inserted, so two first-time writers cannot both insert.

### 7. Camera delete versus an active manual recording — REPRODUCED

Path: delete updates `ACTIVE` manual sessions to `STOPPED`, then deletes the camera (`services/control-api/app/routers/cameras.py:931-941`). `camera_id` is `ON DELETE SET NULL` (`services/control-api/app/models/entities.py:174-176`, migration `0017`). Start inserts an `ACTIVE` session after locking the recording policy and any already-active session (`services/control-api/app/routers/manual_recordings.py:76-99`). It does not lock the camera row. Delete's `UPDATE` locks only sessions that are already `ACTIVE`, so an insert that commits after that update is invisible to it.

Method: delete's manual-session `UPDATE` waits before `session.delete`. There is no pre-existing `ACTIVE` session, so the update locks nothing. Start then inserts and commits an `ACTIVE` session for that camera. Delete resumes, deletes the camera, and commits. Media path deletion is stubbed so the race is the database window, not a live MediaMTX call. `placement_execution_enabled` is false, which is the single-node branch in `delete_camera`. 20/20.

Wrong state: `delete_error` is null, `camera_exists` is false, and the stored session is `{'state': 'ACTIVE', 'camera_id': None, 'stopped_at': 'None'}`. Start's own response, issued before the delete commit, still showed `camera_id='cam-d-0'`.

`VMS-FIX-043-candidate`: take `SELECT ... FOR UPDATE` on the camera row at the start of both delete and manual start, so the insert cannot commit between the stop update and the camera delete. Under that lock, delete's `ACTIVE` update sees the new session and stops it before `ON DELETE SET NULL`. A deleted camera then has no `ACTIVE` manual session.

## Follow-up ids

These ids are proposals for separate fix tasks. This change does not reserve them in the tracker and does not implement the fixes.

| Id | Race |
| --- | --- |
| `VMS-FIX-037-candidate` | Local-event duplicate insert |
| `VMS-FIX-038-candidate` | ONVIF task death after an unexpected unsubscribe |
| `VMS-FIX-039-candidate` | Alarm ACK versus close |
| `VMS-FIX-040-candidate` | Manual list-expiry versus an earlier stop |
| `VMS-FIX-041-candidate` | Out-of-order heartbeat |
| `VMS-FIX-042-candidate` | Recording-health monotonic write |
| `VMS-FIX-043-candidate` | Camera delete versus an active manual recording |
