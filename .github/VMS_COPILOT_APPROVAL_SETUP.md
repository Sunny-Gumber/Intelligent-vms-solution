# Intelligent VMS — GitHub Copilot cloud-agent safe setup

## Scope / current boundary

Repository: `Sunny-Gumber/Intelligent-vms-solution` (public).
Product: `intelligent-vms-v1/`.
This document is **setup guidance**; committing it does **not** activate
repository rules, confirm a GitHub Copilot subscription or enable the agent.

## A. Enable GitHub Copilot cloud agent (GitHub UI, owner)

1. Sign in as `Sunny-Gumber`. Confirm a **paid GitHub Copilot** plan is active
   in GitHub account settings. ChatGPT Plus does not provide this license.
2. Open the repository's **Settings -> Copilot -> Cloud agent** page and verify
   access has not been disabled for the repository.
3. Keep built-in security/code-quality validation enabled.
4. Keep **Require approval for workflow runs** enabled. Every Copilot-authored
   PR must have workflows manually inspected and explicitly approved via
   **Approve and run workflows**. Never enable automatic workflow approval.
5. Do not grant Copilot agent production credentials, deploy tokens, personal
   access tokens or privileged self-hosted runners. Use GitHub-hosted runners.
6. Keep Copilot code-review approvals **out of merge requirements**. Treat all
   AI reviews as advisory; a human owns acceptance.

## B. Protect main in repository Settings -> Rules -> Rulesets

Create an **active branch ruleset** targeting `main` (or use branch protection):

- Require a pull request before merging; no direct pushes.
- Require passing repository **CI + security checks** before merging. Choose
  check names from the existing successful required workflows; don't guess
  names or require checks not actually emitted.
- Require conversations resolved; dismiss stale approvals after new commits.
- Block force pushes and deletions; avoid admin bypass.
- For enforced **one human approval + Code Owner review**, invite a second
  trusted contributor with write access **before** enabling the rule. A
  contributor cannot approve their own PR. On a single-person repo, this can
  prevent the owner from merging owner-authored PRs.
- Once there is a trusted independent reviewer, enable 1 approval + require
  Code Owner review using `.github/CODEOWNERS`. For agent-authored PRs,
  Sunny-Gumber can act as human reviewer. Do not count AI reviews as human.
- Do **not** add Copilot as a broad ruleset bypass actor. Only consider a
  narrow compatibility exception if Copilot cannot write its own PR branch,
  and never grant bypass permission to merge to main.

No ruleset is created by this file; an authorized repository administrator
must activate and verify the rule in GitHub.

## C. First cloud-agent acceptance test (after setup PR is reviewed/merged)

1. In the repository **Issues**, open a low-risk, documentation-only issue.
2. Scope it to exactly one output and disallow production code, workflows,
   repo configuration and any merge/deployment actions.
3. Set **Assignees -> Copilot**, select `VMS Boss` or the default cloud
   agent, choose `main` as base, and start the session.
4. Confirm a dedicated Copilot branch and **draft PR** are created.
5. Inspect **Files changed**, especially workflow files and credential paths,
   before manually approving any Copilot PR workflow run.
6. Inspect test results and obtain the required **human** review; only
   Sunny-Gumber (or the authorized independent human) may merge.
7. Confirm auditability of the issue, Copilot session, PR, reviews and CI.

## D. VMS task safety

- Scope each task to one issue, branch and PR; check for overlap first.
- Read the repository handoff, all 67 coding rules, architecture and
  security/qualification contracts.
- Preserve tenant/site isolation, recording continuity, placement fencing,
  credential hygiene, bounded resources and Alembic migrations.
- Never claim hardware, interoperability, production or 100K qualification
  without external measured evidence.
- Use one coherent remote integration per day except for documented
  security/correctness emergencies.
- **Stop at the owner's approval gate.** No autonomous merging or deployment.

## E. Operational limitations

- GitHub Copilot cloud agent requires a **paid GitHub Copilot plan** and
  is separate from ChatGPT Plus and OpenAI API billing.
- GitHub Copilot **Automations** (scheduled/event driven) currently require a
  private/internal repository; they are not available in public repositories.
  The cloud agent can still work on assigned issues in this public repo.
- Custom agent profiles are under `.github/agents/`; their presence does
  not prove the cloud agent has been enabled for the account.
- Agent instruction text and CODEOWNERS are not security boundaries until
  repository rules and manual workflow approval are actually configured.
