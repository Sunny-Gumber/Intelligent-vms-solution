# Intelligent VMS — Agent Execution & Handoff Protocol

This protocol applies to every repository Copilot custom agent.


## Mandatory coding standard

Every agent must read and obey `intelligent-vms-v1/docs/program/AI_CODING_RULES.md` before designing, editing, testing, or reviewing VMS code.

The rules apply to new code immediately. Existing code is legacy until audited; do not silently copy a known violation into new work. When rules compete, use the rulebook priority: Security -> Correctness/tests/error handling -> Product alignment -> Architecture -> Readability/style.

Every implementation/review handoff must explicitly state:
- coding-rule impact and any intentional exception;
- security/auth/authorization/encryption/PII impact when touched;
- tests added for new logic;
- any legacy violation discovered in edited code.

## Before work

Every agent must read:
1. `intelligent-vms-v1/docs/program/PRODUCTION_EXECUTION_PLAN.md`
2. the relevant GitHub issue/PR
3. current `main`
4. relevant architecture/security/performance docs
5. current migrations/tests/CI

If active work already exists for the milestone, continue/review it instead of creating a duplicate branch or PR.

## Branching

- Never implement directly on `main`.
- One milestone = one active implementation PR unless Boss explicitly splits it.
- Branch from current `main` after dependency PRs are merged.
- If `main` advances with a required dependency, update/rebase before final review.
- Keep a PR draft until implementation + first QA pass are complete.
- Do not merge merely because GitHub says mergeable.

## Required handoff report

Every specialist report to Boss must include:
- objective
- files/components changed or reviewed
- decisions/assumptions
- commands/tests run
- exact PASS/FAIL results
- security/performance/migration impact
- unresolved risks
- next owner

## QA gate

QA is independent from Developer. QA must test negative/failure cases, not only happy path.
A failing gate returns work to Developer/Architect/Security/Performance as appropriate.

## Reviewer gate

Reviewer reads the real diff and surrounding code.
Reviewer must issue PASS, CONDITIONAL PASS, or FAIL with severity.
BLOCKER findings must be fixed before merge.

## Boss gate

Boss verifies:
- dependency order
- no duplicate PR
- green CI
- required specialist reports
- Reviewer decision
- documentation/runbook impact
- production-claim accuracy

Boss status labels:
- Development Accepted
- Release Candidate
- Production Qualified

Only the last one requires external production qualification evidence.

## Performance evidence rule

A formula is not a benchmark.
A synthetic benchmark is not real-hardware certification.
A small test is not proof of 100K media throughput.

Every capacity recommendation must point to measured evidence or be labeled provisional.

## Security rule

Never trade away:
- tenant isolation
- secrets hygiene
- endpoint validation
- service identity scope
- fencing
- safe migration
just to make a test pass.

## Production-safety rule

Automation may create branches, commits, PRs, tests, docs and deployment manifests.
It must not claim a real production deployment succeeded unless it actually executed in the authorized environment and captured evidence.

## Continue-until-blocked behavior

When Boss is told to "continue the production program":
1. finish/review the current active milestone;
2. merge only after gates pass;
3. update the parent issue;
4. select the next unblocked milestone from the Production Execution Program;
5. delegate it;
6. continue until:
   - Release Candidate is achieved, or
   - an external/manual dependency is reached, or
   - a permission/environment blocker prevents further work.

At an external/manual blocker, Boss must create a precise checklist and stop claiming further completion.
