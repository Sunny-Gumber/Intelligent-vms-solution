# VMS Boss — Daily Batch Protocol

## Goal

Allow multiple ChatGPT sessions to collaborate while minimizing GitHub Actions consumption. The default operating model is **many review/work sessions, one coherent remote integration push per day**.

## Start of every chat

1. Read `INTELLIGENT_VMS_START_HERE.md`.
2. Read `PROJECT_HANDOFF.md`, `VMS_BOSS_STATE.md`, this protocol, execution plan, agent protocol, market program and coding rules.
3. Fetch current `main`, open issues, open PRs, active branch head and latest CI.
4. If repository state is newer than the handoff, trust repository state and reconcile the handoff mentally before working.
5. Continue the active milestone; do not duplicate it.

## During the day

Safe without a remote write: code/diff review, architecture/security analysis, test design, issue/PR inspection, CI diagnosis, evidence review and preparation of a coherent patch plan.

A GitHub connector mutation is not local staging. It changes the remote repository. Therefore avoid incremental “try this” writes when the daily integration has not been authorized/selected.

If another chat has already pushed today, continue review/preparation unless a blocker must be fixed immediately.

## Daily integration

Before writing:
- re-fetch the active PR and exact head SHA;
- verify no overlapping PR/branch appeared;
- reconcile all known findings;
- ensure the batch belongs to the current milestone;
- prepare deterministic positive/edge/negative tests with fixes.

Integrate as one coherent commit where tooling permits, then let CI/security run once on that head. Do not create artificial commits merely to update status text.

After integration:
- record exact new SHA;
- inspect executable CI evidence;
- update PR/issue evidence;
- keep draft/block status if required gates are unavailable or failing;
- update `VMS_BOSS_STATE.md` in the same integration batch where practical.

## Emergency exception

An additional same-day push is allowed for a newly discovered security/correctness defect, broken migration, data-loss risk, recording-continuity risk, or a fix required by an actually executed gate. Document why the exception was necessary.

## Multi-chat concurrency rule

Two chats must never independently mutate the same active branch from the same old head. Every writer must re-fetch the current head immediately before integration. If it changed, stop, inspect the newer diff and rebase/reconcile conceptually before any write.

## CI policy

Do not infer code failure from a workflow that never started executable steps. Conversely, local/static review never substitutes for required executable CI. Merge only after the program's required gates and independent QA/Reviewer decisions are evidenced.

## Qualification boundary

Software completion is not hardware qualification. Named camera/model/firmware, site, benchmark and production-security claims remain external-evidence dependent.
