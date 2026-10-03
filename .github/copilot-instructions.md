# Repository Copilot Instructions

For any task touching `intelligent-vms-v1/`, first read:
- `intelligent-vms-v1/docs/program/PRODUCTION_EXECUTION_PLAN.md`
- `intelligent-vms-v1/docs/program/AGENT_EXECUTION_PROTOCOL.md`
- `intelligent-vms-v1/docs/program/AI_CODING_RULES.md`

These documents define phase order, agent responsibilities, acceptance gates, production-claim rules, and the mandatory 67-rule coding standard. Apply the coding rules to every new or modified line; audit legacy code before relying on it.

Use repository custom agents under `.github/agents/`. For cross-phase work, use **VMS Boss** as coordinator and delegate to specialists.

Do not create a second implementation PR for a milestone that already has active work. Inspect open PRs/issues first.

Never mark 100K capacity, hardware sizing, GPU throughput or production qualification complete without the evidence required by the Production Execution Program.
