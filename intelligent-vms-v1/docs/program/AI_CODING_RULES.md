# AI Coding Assistant — Complete Rule List
*Ordered smallest → biggest: from a single line of code up to full product/architecture decisions.*

---

## LEVEL 1 — Syntax & Formatting (smallest)
1. Follow the project's existing indentation, spacing, and bracket style — never reformat untouched code.
2. Match the existing quote style (single/double), semicolon usage, and line-length limit.
3. Use the language's standard linter/formatter config if one exists (e.g. ESLint, Prettier, Black, gofmt) — don't invent your own style.
4. Keep line length readable (~80–120 chars depending on project convention).
5. No trailing whitespace, no mixed tabs/spaces.

## LEVEL 2 — Naming
6. Variable, function, and class names must be descriptive, not abbreviated cryptically (`userCount`, not `uc`).
7. Follow the project's casing convention (camelCase, snake_case, PascalCase) consistently per language.
8. Boolean variables/functions should read as a question (`isValid`, `hasPermission`).
9. Avoid generic names (`data`, `temp`, `x`) except in tiny, obvious scopes (loop counters).
10. Name things by what they *are/do*, not by implementation detail that might change.

## LEVEL 3 — Single Function / Unit Rules
11. Each function should do one thing; if it needs "and" to describe it, split it.
12. Keep functions short enough to read on one screen; extract helpers otherwise.
13. Validate inputs at the boundary of a function — don't assume callers are well-behaved.
14. Avoid deep nesting (>3 levels) — use early returns/guard clauses instead.
15. No magic numbers or strings — use named constants.
16. Pure functions preferred where possible (same input → same output, no hidden side effects).

## LEVEL 4 — Error Handling
17. Never silently swallow errors — log, handle, or re-throw with context.
18. Fail loudly in development, gracefully in production (user-facing messages ≠ stack traces).
19. Use the language's standard error/exception mechanism — don't invent custom control flow for errors.
20. Distinguish expected failures (bad input, network timeout) from bugs (null pointer, logic error) in how they're handled.
21. Always clean up resources (files, connections, locks) even on failure — use `finally`/`defer`/context managers.

## LEVEL 5 — Comments & Documentation
22. Comment *why*, not *what* — the code already shows what; explain non-obvious reasoning, trade-offs, or gotchas.
23. Every public function/class gets a docstring: purpose, parameters, return value, exceptions.
24. Flag any assumption made while writing the code (`// assuming X is always non-null because...`).
25. Update existing comments/docs when you change the code they describe — never leave stale docs.
26. No commented-out dead code left behind.

## LEVEL 6 — Testing
27. New logic gets a test unless explicitly told not to.
28. Cover the happy path, at least one edge case, and one failure case per function.
29. Tests must be deterministic — no reliance on real time, real network, or random values without seeding/mocking.
30. Don't modify existing tests to make new code pass — fix the code, or flag the conflict.
31. Use the project's existing test framework/patterns; don't introduce a second one.

## LEVEL 7 — File / Module Organization
32. New files go where the existing structure implies they should — mirror existing patterns.
33. One clear responsibility per file/module.
34. Keep public interfaces (exports) minimal — don't expose internals that don't need to be external.
35. Group related functionality; avoid scattering one feature's code across unrelated folders.

## LEVEL 8 — Dependencies
36. Don't add a new library for something the project (or language standard library) already solves.
37. If a new dependency is genuinely needed, name it explicitly and explain why before adding it.
38. Pin versions consistent with the project's existing dependency-management approach.
39. Never introduce a dependency with known security advisories without flagging it.

## LEVEL 9 — Security
40. Never hardcode secrets, API keys, or credentials — use the project's config/env-variable pattern.
41. Sanitize/validate all external input (user input, API responses, file contents) before use.
42. Use parameterized queries — never string-concatenate SQL or shell commands.
43. Apply least privilege — request/use only the access a piece of code actually needs.
44. Flag anything that touches authentication, authorization, encryption, or PII explicitly in the response, even if not asked.

## LEVEL 10 — Performance
45. Don't optimize prematurely — write clear code first, then optimize only where there's a known bottleneck.
46. Watch for obvious inefficiencies: N+1 queries, unnecessary loops inside loops, repeated expensive calls.
47. Consider memory/resource usage for anything handling large data (files, video, images, big datasets).

## LEVEL 11 — Version Control Hygiene
48. Keep changes scoped to the task — don't refactor unrelated code in the same change.
49. Write a clear, specific commit-message-style summary of what changed and why.
50. Call out breaking changes explicitly.

## LEVEL 12 — API / Interface Design
51. Design interfaces (functions, classes, endpoints) for the caller's convenience, not the implementer's.
52. Keep backward compatibility unless a breaking change is explicitly requested — and flag it if one is unavoidable.
53. Consistent error/response shape across an API — don't mix conventions.
54. Version APIs when making breaking changes, rather than silently changing behavior.

## LEVEL 13 — Data Handling
55. Be explicit about data types, especially at boundaries (API, database, file I/O).
56. Handle nulls/missing fields explicitly — don't assume data is always complete.
57. Respect data-retention, privacy, and compliance rules relevant to the product domain.
58. Never log sensitive data (passwords, tokens, personal data) even for debugging.

## LEVEL 14 — Architecture & Design Principles
59. Respect the existing architecture (layers, patterns, boundaries) unless a change is explicitly requested.
60. Don't introduce a new architectural pattern (e.g. new state-management approach, new folder convention) without flagging it as a decision, not a silent choice.
61. Keep coupling low — changes in one module shouldn't force changes across many others.
62. Design for the scale the product actually needs — don't over-engineer for hypothetical scale, don't under-engineer for known scale.

## LEVEL 15 — Product & Business Alignment (biggest)
63. Understand what the feature is *for* — the user problem it solves — not just its technical spec.
64. Flag when a request conflicts with existing product behavior, other features, or known business rules.
65. Call out trade-offs that affect the product (cost, latency, user experience) before implementing silently.
66. When requirements are ambiguous, state the assumption made and proceed — don't guess silently on anything that affects users or data.
67. Always explain, in plain language, what was built/changed and why — a person should be able to review it without reading every line of code.

---

## Quick-Reference Order of Priority
If time/scope is limited, priority order is: **Security → Correctness (tests/error handling) → Product alignment → Architecture → Readability/style.** Never trade security or correctness for speed or style.
