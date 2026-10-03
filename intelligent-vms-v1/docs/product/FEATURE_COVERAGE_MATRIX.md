# VMS Feature Coverage Matrix

Authoritative capability source:
`docs/product/VMS_COMPLETE_FEATURE_MASTER_CHECKLIST.md`

This matrix must eventually contain **one row for every written capability** in
that source. Do not merge multiple independent capabilities merely to improve a
coverage percentage.

## Columns

| Field | Required |
|---|---|
| Feature ID | Yes |
| Maturity level | Yes |
| Category ID / category | Yes |
| Exact feature name | Yes |
| Acceptance criteria | Yes |
| Support mode (N/C/I/A/L/CL/OP/P/R/NS/NV) | Yes |
| Engineering state | Yes |
| Native/integrated route | Yes |
| Camera/device dependency | Yes |
| Server/AI dependency | Yes |
| Licence/module | Yes |
| On-prem | Yes |
| Cloud | Yes |
| Desktop | Yes |
| Mobile | Yes |
| Web | Yes |
| API | Yes |
| Maximum measured scale | When applicable |
| Minimum version | When supported |
| Tested version | When verified |
| Automated-test reference | When software-verifiable |
| Supporting document | Yes when verified |
| Evidence reference | Yes when verified |
| Verification status | Yes |
| Known limitations | Yes |
| Owner/workstream | Yes |
| Last verification date | Yes when verified |

## Initial rule

Newly imported capabilities start as:

- support mode: `R` unless existing repository evidence proves a stronger code;
- engineering state: `TARGET`;
- verification: `NV`.

Existing functionality must be audited against the repository before changing a
row to N/P/etc. Do not infer support from architecture alone.

## Completion rule

A row can become VERIFIED only when its acceptance criteria and relevant
evidence are present. For device/certification/infrastructure-dependent rows,
external evidence is mandatory.
