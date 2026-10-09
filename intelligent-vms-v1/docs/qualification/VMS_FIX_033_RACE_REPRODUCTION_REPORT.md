# VMS-FIX-033 race reproduction report

Issue: [#106](https://github.com/Sunny-Gumber/Intelligent-vms-solution/issues/106).

This pass reproduces the seven conditional races. It does not patch them. Every reproduced test asserts the correct outcome, fails on this tree, and is marked `known_race`. The marker is deselected from the default suite in `tests/conftest.py`. Nothing is xfailed. The assertions do not encode the buggy row.

Reproductions ran on a disposable local PostgreSQL 16.15 database (`vms_fix_033` on `127.0.0.1`) after `alembic upgrade head`. The head revision is **`0019`** (`0019_camera_source_protocol.py`, which revises `0018`). `0019` adds `cameras.source_protocol` (server default `rtsp`) and `source_fingerprint`. It does not change event history, alarm state, manual-session expiry, heartbeat assignment, recording-health writes, or `ON DELETE SET NULL`. `SHOW transaction_isolation` was **read committed** for every PostgreSQL case. The ONVIF case drives `services/onvif-event-worker/main.py` and the real `unsubscribe` function; it does not use a database.

This branch merges `main` at `cde62ad4b9d02a6bac97b6788bd1a9a45cb8f973` (VMS-FIX-032). The race handlers' lost-update bodies are not modified by this pull request. `heartbeat_node`'s route dependency is now `require_global_admin(allow_node_service=True)`; the function still assigns load, authority mode, and `heartbeat_at` from the payload with no comparison to the stored timestamp. The reproduction calls that function with an admin principal whose `tenant_id` is `*`.

## How to run

Default suite (known races are deselected, not skipped):

```bash
cd intelligent-vms-v1
pip install -r tests/requirements.txt
VMS_SECRET_KEY=ci-test-key python3 -m pytest -q tests
```

Reproduced races. These fail on the current handlers. Point the URL at a disposable database; the tests migrate it and truncate application tables, including after the last attempt and on fixture teardown.

```bash
cd intelligent-vms-v1
VMS_SECRET_KEY=ci-test-key \
VMS_TEST_POSTGRES_URL='postgresql+asyncpg://USER:PASSWORD@127.0.0.1:5432/DATABASE' \
  python3 -m pytest -m known_race -q tests/test_vms_fix_033_race_reproductions.py
```

`VMS_ALARM_TRANSACTION_TEST_DATABASE_URL` is accepted as the existing equivalent. With neither variable set, each PostgreSQL reproduction skips:

`PostgreSQL race reproductions need VMS_TEST_POSTGRES_URL or VMS_ALARM_TRANSACTION_TEST_DATABASE_URL`

To un-deselect one race when its fix lands: remove `@pytest.mark.known_race` from that one test. Do not use `xfail`, `skip`, or a new blanket marker. Leave the conftest hook until every `known_race` marker is gone. Then run `pytest -q tests` and confirm that test is in the passed count and the deselected count dropped by one. A node id without `-m known_race` exits 5 while the marker remains.

## Harness

Every barrier wait and every attempt is bounded. `COMPETITOR_TIMEOUT` is 3 seconds, `HOLDER_TIMEOUT` is 10 seconds, and `ATTEMPT_TIMEOUT` is 20 seconds. The engine sets PostgreSQL `lock_timeout` to 8 seconds and `statement_timeout` to 15 seconds, so a forgotten release fails the attempt instead of hanging the process.

The holder pauses after it has taken the row it needs to protect. The competitor is given 3 seconds. If it finishes, it did not need that lock and has already committed: that is the lost-update interleaving, and the assertion still requires the correct final state. If it is still running, it is waiting on the holder's lock. The harness then releases the holder, the holder commits, and the blocked side continues. That blocked-then-serialised order is a valid correct outcome. It is not recorded as a timeout.

A blocked side that still ends in the wrong state fails. The current code does not take the locks, so these tests stay on the non-blocked path and fail on the same wrong final states as before.

## Results

| # | Item | Classification | Attempts | Isolation | Observed wrong state | Follow-up |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Local-event duplicate insert | REPRODUCED | 20/20 | read committed | One row stored. The other caller raises `UniqueViolation` on `event_history_pkey` instead of returning `False`. | `VMS-FIX-038` |
| 2 | ONVIF task death after unexpected unsubscribe | REPRODUCED | 20/20 | n/a (worker task) | Camera task ends with `TargetNotAllowed`. `pull` stays 1, `create` stays 1, and the supervisor does not replace it. | `VMS-FIX-039` |
| 3 | Alarm ACK versus close | REPRODUCED | 20/20 | read committed | Close commits `closed`. The stale acknowledge then commits `acknowledged` (HTTP 200). Final state is `acknowledged`. | `VMS-FIX-040` |
| 4 | Manual list-expiry versus an earlier stop | REPRODUCED | 20/20 | read committed | Explicit stop stores `stopped_at=12:00:40Z`. List expiry then stores `12:01:40Z` (`max_stop_at`). | `VMS-FIX-041` |
| 5 | Out-of-order heartbeat | REPRODUCED | 20 sequential and 20 concurrent, 40/40 | read committed | Older `observed_at` wins: `heartbeat_at=12:00:05Z`, `load.cpu=9`, `authority_mode=regional_autonomous`. | `VMS-FIX-042` |
| 6 | Recording-health monotonic write | REPRODUCED | 20/20 | read committed | Newer completion `seg-later` at `12:00:20Z` is overwritten by `seg-earlier` at `12:00:10Z`. | `VMS-FIX-043` |
| 7 | Camera delete versus an active manual recording | REPRODUCED | 20/20 | read committed | Delete succeeds and the camera row is gone. The manual session stays `ACTIVE` with `camera_id` NULL and `stopped_at` NULL. | `VMS-FIX-044` |

A control, not a race, is in the default suite: `test_onvif_known_unsubscribe_error_retries`. The real `unsubscribe` swallows `OnvifError`. Across 3/3 attempts the camera task stayed alive and pulled again (`pull=2`, `create=2`). That test passed (`1 passed in 0.68s` on this tree, before the gate run below).

`VMS-FIX-037` is already issue #111. These seven follow-ups are `VMS-FIX-038` through `VMS-FIX-044`.

## Commands and counts

Default suite, without a Postgres URL:

`1514 passed, 7 deselected, 2 warnings in 63.79s`

The 7 deselected tests are the `known_race` reproductions. The two warnings are pre-existing HMAC key-length warnings in `tests/test_auth_phase2b.py`. The count includes the merge of `main` at `cde62ad`.

Postgres reproductions (`VMS_TEST_POSTGRES_URL` set, `-m known_race`):

`7 failed, 1 deselected in 27.02s`

Exit code 1. The deselected test is the passing ONVIF `OnvifError` control, which does not carry `known_race`. After that run, `event_history`, `alarm_instances`, `manual_recording_sessions`, `recording_health_state`, `cameras`, and `infrastructure_nodes` each had 0 rows. `alembic_version` was `0019`.

`ruff check services tools tests` passed (`All checks passed!`). `python3 tools/check_public_docstrings.py` passed (`Public-interface docstring gate OK`).

## Fix-shape probes

The probes below were local and uncommitted. Each one patched only the cited production function, ran that race's test at `ATTEMPTS = 20`, then `git checkout --` restored the file. After the last restore, `git diff` on the production paths was empty. None of the runs hung. A green lock-based run takes about 64 seconds because each of the 20 attempts waits the 3-second competitor budget before the holder is released. That wait is the serialised path, and the test then passes.

Command shape, from `intelligent-vms-v1`:

```bash
VMS_SECRET_KEY=ci-test-key \
VMS_TEST_POSTGRES_URL='postgresql+asyncpg://vms:vms@127.0.0.1:5432/vms_fix_033' \
  python3 -m pytest -m known_race -q tests/test_vms_fix_033_race_reproductions.py::<test> --tb=line
```

| Race | Fix applied, then reverted | Green | Red after revert |
| --- | --- | --- | --- |
| 1 Local event | `INSERT ... ON CONFLICT DO NOTHING` on `event_id`, no `session.get`, return `bool(rowcount)`. Same shape as `enqueue_message_once`. | PASS, 64.45s wall, pytest `1 passed in 64.05s` | FAIL, 4.80s wall, pytest `1 failed in 4.41s`. `UniqueViolation` on `evt-0`, winner `True`. |
| 2 ONVIF | `except Exception` around `unsubscribe` in `camera_loop`'s `finally`. | PASS, 1.14s wall, pytest `1 passed in 0.77s` | FAIL, 1.55s wall, pytest `1 failed in 1.17s`. `TargetNotAllowed`, `live_camera_tasks=0`, `pull=1`. |
| 3 Alarm | `session.get(..., with_for_update=True)` on acknowledge and close. | PASS, 64.98s wall, pytest `1 passed in 64.56s` | FAIL, 4.45s wall, pytest `1 failed in 4.05s`. Final `acknowledged`. |
| 3 Alarm | Conditional `UPDATE ... WHERE state='open'`. Zero rows and a refreshed `closed` row return HTTP 409. No ORM assign before the update. | PASS, 4.33s wall, pytest `1 passed in 3.93s` | FAIL, 5.11s wall, pytest `1 failed in 4.71s`. Final `acknowledged`. |
| 4 Manual list | `SELECT ... FOR UPDATE` on the active-session list, then the existing expiry write. | PASS, 64.87s wall, pytest `1 passed in 64.45s` | FAIL, 5.25s wall, pytest `1 failed in 4.85s`. Midpoint `12:00:40Z`, final `12:01:40Z`, `blocked` false. |
| 4 Manual list | `UPDATE ... WHERE state='ACTIVE'` after the unlocked select. The loaded ORM row is not assigned. | PASS, 4.08s wall, pytest `1 passed in 3.68s` | FAIL, 4.54s wall, pytest `1 failed in 4.14s`. Same `12:01:40Z` overwrite. |
| 5 Heartbeat | `UPDATE ... WHERE heartbeat_at < :clamped` for load, authority mode, and `heartbeat_at`. No ORM assign. | PASS, 70.80s wall, pytest `1 passed in 70.38s` | FAIL, 8.34s wall, pytest `1 failed in 7.91s`. 20 sequential and 20 concurrent stored `12:00:05Z`, `cpu=9`, `regional_autonomous`. |
| 6 Recording health | Conditional `UPDATE` after the existing `session.get`, predicate `last_segment_completed_at IS NULL OR < :completed_at`. No ORM assign. | PASS, 4.52s wall, pytest `1 passed in 4.12s` | FAIL, 4.46s wall, pytest `1 failed in 4.06s`. `seg-earlier` at `12:00:10Z`. |
| 7 Camera delete | `authorized_camera` uses `session.get(..., with_for_update=True)`, so delete and start lock the camera. | PASS, 65.05s wall, pytest `1 passed in 64.66s` | FAIL, 4.61s wall, pytest `1 failed in 4.22s`. `delete_error` null, camera gone, stored `ACTIVE` with `camera_id` NULL. |
| 7 Camera delete | Second `ACTIVE` stop with a fresh `datetime.now()` before `session.delete`. | PASS, 4.87s wall, pytest `1 passed in 4.46s` | FAIL, 4.96s wall, pytest `1 failed in 4.55s`. Same orphan `ACTIVE` row. |

Every documented recipe above is accepted. A recipe was not left red, and none had to be narrowed to a single shape.

Shapes this harness accepts, by race:

1. `ON CONFLICT DO NOTHING` with no `session.get`. A fix that still calls `session.get` and then inserts idempotently is still interleaved by that get; this pass proved the no-get shape.
2. Catching `Exception` around `unsubscribe` in `camera_loop`. The injected fault is `TargetNotAllowed` (`ValueError`). `except RuntimeError` does not catch it, because `OnvifError` is the `RuntimeError` subclass and `TargetNotAllowed` is not.
3. `FOR UPDATE` on both alarm loads, and a conditional acknowledge `UPDATE ... WHERE state='open'` that returns 409 when the refreshed row is `closed`. Final state must be `closed`. HTTP 200 from acknowledge is acceptable when close runs after it.
4. `FOR UPDATE` on the active list, and a conditional `UPDATE ... WHERE state='ACTIVE'` that does not assign the stale ORM object. If stop blocks, list expires under the lock and a final `STOPPED` row passes. If stop commits first, `stopped_at` must stay at `12:00:40Z`.
5. The conditional heartbeat `UPDATE`. The ORM object must not stay dirty, or commit overwrites the predicate. An older unconditional assignment that commits last stays red.
6. The conditional recording-health `UPDATE` after `session.get`, without assigning the ORM fields.
7. `FOR UPDATE` on the camera in `authorized_camera`, and a second `ACTIVE` stop that uses a fresh `datetime.now()` before `session.delete`. Success requires `delete_error is None`, the camera row gone, no `ACTIVE` session, and every stored session `STOPPED`. A missing row is success when start runs only after the camera is gone. Reusing the pre-gap `now` is not a valid fix: the new session's `started_at` is later, and `ck_manual_recording_stop_bounds` rejects `stopped_at < started_at`.

## Per-item evidence

### 1. Local-event duplicate insert — REPRODUCED

Path: `persist_local_event_once` loads by primary key and, on a miss, `session.add`s the row (`services/control-api/app/services/local_event_store.py:12-25`). Callers commit afterwards (`services/control-api/app/routers/events.py:121-125`). The health monitor calls the same helper inside its own transaction (`services/control-api/app/services/health_monitor.py:507-508`). `event_history.event_id` is the primary key (`migrations/versions/0018_local_event_history.py:37`).

Method: two sessions call the real helper with one `event_id`. The first `AsyncSession.get` waits before insert. The second get also misses, inserts, and commits. The first then inserts and commits. If `session.get` is never called, both inserts run and the outcome is checked; that path is the `ON CONFLICT` shape, not a barrier failure. 20/20 on the current helper.

Wrong state: `count=1`. The winner returns `True`. The loser raises `IntegrityError` / `UniqueViolationError` on `event_history_pkey` (`Key (event_id)=(evt-0) already exists`). It does not return `False`. The primary key stops a second row and aborts the loser's transaction, so a duplicate retry is not idempotent. The outbox path already avoids this with `ON CONFLICT DO NOTHING` (`services/control-api/app/services/outbox.py:180-196`).

`VMS-FIX-038` (P2): insert the local event with `INSERT ... ON CONFLICT DO NOTHING` on `event_id` and return `False` when `rowcount` is 0, the same shape as `enqueue_message_once`. That keeps the surrounding transaction usable, including the health-monitor write that calls this helper.

### 2. ONVIF task death after an unexpected unsubscribe — REPRODUCED

Path: `camera_loop` catches a pull failure, then its `finally` always calls `unsubscribe` (`services/onvif-event-worker/main.py:285-288`). `unsubscribe` catches only `OnvifError` and documents `TargetNotAllowed`, `OSError`, and other exceptions as propagating (`services/control-api/app/services/onvif_events.py:300-326`). `OnvifError` subclasses `RuntimeError` (`services/control-api/app/services/onvif_client.py:36`). `TargetNotAllowed` subclasses `ValueError` (`services/control-api/app/services/network_policy.py:12`). The supervisor records the task in a dict and starts a new one only when the camera id is absent (`services/onvif-event-worker/main.py:312-324`). A finished task stays in that dict.

Method: the real `supervisor` and real `unsubscribe`. `pull_messages` raises `ConnectionError` once. `_soap` inside `unsubscribe` raises `TargetNotAllowed`. The test waits on a counter the supervisor loop and the unsubscribe path both mark. It does not sleep to decide the outcome. Production reconnect backoff of 1 second or more is still compressed to 0.01 seconds so the retry delay is not the observation. 20/20.

Wrong state: one camera task, `live_camera_tasks=0`, `task_error='TargetNotAllowed: synthetic unsubscribe target is outside site policy'`, `pull=1`, `unsub=1`, `create=1`. No replacement task is created.

Control, 3/3, default suite: `_soap` raises `OnvifError`. The real handler logs and returns. The loop reconnects (`pull=2`, `create=2`) and the camera task stays live.

`VMS-FIX-039` (P2): catch unexpected unsubscribe failures inside the `camera_loop` `finally`, log them, and continue the reconnect loop. In the supervisor, drop a task that is already done and start a replacement for that camera. Known `OnvifError` cleanup can stay best-effort. Catching only `RuntimeError` would turn a `RuntimeError` injection green and would still leave `TargetNotAllowed` and `OSError` killing the task.

### 3. Alarm ACK versus close — REPRODUCED

Path: acknowledge loads the row, rejects `state == "closed"`, then writes `acknowledged` and commits (`services/control-api/app/routers/alarms.py:494-506`). Close loads the same row, sets `closed`, and commits (`services/control-api/app/routers/alarms.py:528-534`). Neither statement uses `FOR UPDATE` or a compare-and-set predicate. VMS-FIX-002's rule-write fencing is a different path and does not cover these two handlers.

Method: acknowledge's `session.get` waits after it has loaded `open` and, when that get is `FOR UPDATE`, after it holds the row lock. Close then runs. If close blocks on the lock, the harness lets acknowledge commit and then lets close continue. On the current code close does not block. 20/20 at read committed.

Wrong state: close returns HTTP 200 with `state=closed`. Acknowledge then returns HTTP 200 with `state=acknowledged`. The stored final state is `acknowledged`.

`VMS-FIX-040` (P1): serialize both handlers with `SELECT ... FOR UPDATE`, and make acknowledge a conditional update `SET state='acknowledged' WHERE id=:id AND state='open'`. If that updates zero rows, re-read and return HTTP 409 when the alarm is already closed. Close remains the terminal write. The test requires the stored state to be `closed`.

Constraint **REV-033-101 / QA-033-101**: the reproduction barrier hooks `session.get`. Both handlers take the lock with `session.get(AlarmInstanceEntity, alarm_id, with_for_update=True)`. A lock written as `session.execute(select(...).with_for_update())` stays red against this barrier; it is bounded and does not hang. Do not change the barrier to make a different lock shape pass unless QA and the Reviewer agree it is still a faithful reproduction. VMS-FIX-040 removes `@pytest.mark.known_race` from `test_alarm_close_is_not_overwritten_by_stale_acknowledge` only. The other six reproductions keep the marker.

### 4. Manual list-expiry versus an earlier stop — REPRODUCED

Path: list loads `ACTIVE` rows without a lock, then `_finalize_if_expired` sets `STOPPED` and `stopped_at=max_stop_at` when `max_stop_at <= now` (`services/control-api/app/routers/manual_recordings.py:45-51` and `:133-140`). Stop locks the row by id and sets `stopped_at=min(now, max_stop_at)` (`services/control-api/app/routers/manual_recordings.py:191-197`).

Method: the barrier matches the list statement because its `WHERE` clause filters on `state`, including when that select is `FOR UPDATE`. Stop's id-only `FOR UPDATE` still projects the `state` column and does not match. The list `SELECT` waits before it reads the clock. Stop commits `stopped_at=2026-10-08T12:00:40Z` (40 seconds after start, before `max_stop_at`). The list clock then returns `max_stop_at` (`12:01:40Z`) and finalizes the stale `ACTIVE` object. If stop blocks on the list lock, the list clock moves to `max_stop_at` before the holder continues, so list expires under the lock. 20/20 on the current unlocked list.

Wrong state: a read between the two commits shows `STOPPED` at `12:00:40Z`. After the list commits, `stopped_at` is `12:01:40Z`. `blocked` is false. The earlier stop is lengthened to the cap.

`VMS-FIX-041` (P2): lock the session with `FOR UPDATE` before expiry, re-read `state` under that lock, and write `stopped_at` only while the locked row is still `ACTIVE`. An already `STOPPED` row keeps the earlier `stopped_at`. A conditional `UPDATE ... WHERE state='ACTIVE'` after the unlocked select is the other shape this test accepts, provided the stale ORM object is not assigned.

### 5. Out-of-order heartbeat — REPRODUCED

Path: `heartbeat_node` assigns `load_json`, `authority_mode`, and `heartbeat_at = min(observed_at, now)` from the payload alone (`services/control-api/app/routers/placement.py:134-145`). It does not compare `observed_at` with the stored `heartbeat_at`. The clamp stops a future clock from looking fresher than wall time. It does not stop an older observation from overwriting a newer one. `node_eligible` returns false unless `authority_mode == "central_online"` (`services/control-api/app/services/placement.py:128-129`). This report states the stored row. That stored row is enough to make the node ineligible. It does not claim a cluster outage.

Method: base `heartbeat_at` is `12:00:00Z`. The newer payload is `observed_at=12:00:20Z`, `cpu=1`, `central_online`. The older payload is `observed_at=12:00:05Z`, `cpu=9`, `regional_autonomous`. Sequential: newer commits, then older runs to completion. Concurrent: the older transaction pauses on its commit. If the newer update blocks on the row lock that pause is holding, the harness releases the older commit and lets the newer update continue. On the current code the newer update does not block. 20 sequential and 20 concurrent, all at read committed.

Wrong state, both schedules: stored `heartbeat_at=2026-10-08T12:00:05Z`, `load={'cpu': 9.0}`, `authority_mode='regional_autonomous'`.

`VMS-FIX-042` (P1): update the node with a single conditional statement whose `WHERE` requires the clamped `observed_at` to be strictly newer than `heartbeat_at`. Leave `load_json`, `authority_mode`, and `heartbeat_at` unchanged when the incoming observation is older or equal. Do not leave the ORM object dirty. Return the stored row either way.

### 6. Recording-health monotonic write — REPRODUCED

Path: `record_segment_completion` loads the health row, and if `completed_at` is earlier than the in-memory `last_segment_completed_at` it returns without writing (`services/control-api/app/services/recording_health.py:126-136`). Otherwise it assigns the new segment fields (`:138-146`). The check is not a locked read and not a conditional `UPDATE`.

Method: the row starts at `seg-base` / `12:00:00Z`. The earlier completion (`seg-earlier`, `12:00:10Z`) waits after `session.get`. The later completion (`seg-later`, `12:00:20Z`) reads the same base, writes, and commits. The earlier completion then writes and commits. If the later writer blocks on a lock held by that get, the harness lets the earlier writer finish and then lets the later writer continue. Both timestamps are after the base, so both pass the in-memory check. 20/20 on the current code, and the later writer does not block.

Wrong state: `last_segment_id='seg-earlier'`, `last_segment_completed_at=2026-10-08T12:00:10Z`.

`VMS-FIX-043` (P2): perform the advance as `UPDATE recording_health_state SET ... WHERE camera_id=:id AND (last_segment_completed_at IS NULL OR last_segment_completed_at < :completed_at)`. If the predicate matches nothing, leave the newer row in place and do not assign the ORM object. Pair it with `SELECT ... FOR UPDATE` when the row must be inserted, so two first-time writers cannot both insert.

### 7. Camera delete versus an active manual recording — REPRODUCED

Path: delete updates `ACTIVE` manual sessions to `STOPPED`, then deletes the camera (`services/control-api/app/routers/cameras.py:931-941`). `camera_id` is `ON DELETE SET NULL` (`services/control-api/app/models/entities.py:174-176`, migration `0017`). Start inserts an `ACTIVE` session after locking the recording policy and any already-active session (`services/control-api/app/routers/manual_recordings.py:71-99`). It does not lock the camera row. `authorized_camera` is an unlocked `session.get` (`services/control-api/app/routers/cameras.py:142`). Delete's `UPDATE` locks only sessions that are already `ACTIVE`, so an insert that commits after that update is invisible to it.

Method: delete's manual-session `UPDATE` waits before `session.delete`. There is no pre-existing `ACTIVE` session, so the update locks nothing. Start then inserts and commits an `ACTIVE` session for that camera. Delete resumes, deletes the camera, and commits. If start blocks on a camera lock held by delete, the harness lets delete commit and then lets start run against the deleted camera. Media path deletion is stubbed so the race is the database window, not a live MediaMTX call. `placement_execution_enabled` is false, which is the single-node branch in `delete_camera`. 20/20 on the current code, and start does not block.

Wrong state: `delete_error` is null, `camera_exists` is false, and the stored session is `{'state': 'ACTIVE', 'camera_id': None, 'stopped_at': 'None'}`. Start's own response, issued before the delete commit, still showed `camera_id='cam-d-0'`. The assertion requires the opposite: `delete_error is None`, the camera row gone, no `ACTIVE` row, and every stored row `STOPPED`. A rolled-back delete plus a normal `ACTIVE` session does not pass. No stored row is success when start runs only after the camera is gone.

`VMS-FIX-044` (P1): take `SELECT ... FOR UPDATE` on the camera row at the start of both delete and manual start, so the insert cannot commit between the stop update and the camera delete. Under that lock, delete's `ACTIVE` update sees the new session and stops it before `ON DELETE SET NULL`. The other shape this test accepts is a second `ACTIVE` stop, using a fresh `datetime.now()` taken after the gap, immediately before `session.delete`. Do not reuse the pre-gap clock.

Each attempt truncates the application tables before the next attempt, and the module truncates again after the last attempt and on fixture teardown.

## Follow-up ids

`VMS-FIX-037` is issue #111 and is not reused. These ids are the next free numbers. This change does not implement the fixes. Priority follows the product impact of the wrong state, not the race number.

| Id | Priority | Race |
| --- | --- | --- |
| `VMS-FIX-042` | P1 | Out-of-order heartbeat |
| `VMS-FIX-040` | P1 | Alarm ACK versus close |
| `VMS-FIX-044` | P1 | Camera delete versus an active manual session |
| `VMS-FIX-039` | P2 | ONVIF task death after an unexpected unsubscribe |
| `VMS-FIX-041` | P2 | Manual list-expiry versus an earlier stop |
| `VMS-FIX-043` | P2 | Recording-health monotonic write |
| `VMS-FIX-038` | P2 | Local-event duplicate insert |
