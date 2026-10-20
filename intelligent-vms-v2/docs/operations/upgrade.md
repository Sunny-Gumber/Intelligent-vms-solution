# Upgrade Policy and Procedure

## Compatibility rules

| Change | Policy |
| --- | --- |
| API within `/api/v1` | Additive only. Fields and endpoints may be added; none removed or renamed. |
| Breaking API change | Requires `/api/v2` and a documented deprecation window. |
| Contract enums | Members may be added, never removed or renamed within a major version. |
| Database schema | Forward migration always provided. A downgrade exists for the previous release only. |
| Configuration | New keys get safe defaults. Removed keys are warned about for one release before removal. |

## Version skew

| Combination | Supported |
| --- | --- |
| Newer API, older recorder | Yes, for one minor version. The recorder reads desired state from the database; additive columns do not break it. |
| Older API, newer recorder | **No.** Run the migration first. |
| Newer clients, older API | Degraded. Clients must handle `404` on unknown endpoints. |
| Older clients, newer API | Yes. This is the point of the additive-only rule. |

## Single-server upgrade

```bash
sudo ./scripts/upgrade.sh /path/to/new/source
```

The script backs up first, stops the **recorder before the API** so open
segments are finalized, migrates, restarts, and verifies `/readyz`. If the
migration fails it restarts the previous version and exits non-zero rather
than leaving a half-upgraded system running.

## Interrupted upgrade

Alembic migrations run in a transaction on PostgreSQL, so an interruption
during the migration rolls back. The dangerous window is between "packages
installed" and "migration applied".

If an upgrade is interrupted:

1. Do not start the services.
2. Check the current revision: `alembic current`.
3. If it is behind the new code, run `alembic upgrade head`.
4. If the new code cannot start, reinstall the previous wheels from your
   artefact store and run `alembic downgrade <previous>`.
5. If the database is in an unknown state, restore the pre-upgrade backup.

Startup refuses to run against a schema older than
`REQUIRED_SCHEMA_REVISION` in `app/version.py`, so a mismatched system fails
loudly instead of corrupting data.

## Rolling upgrade

**Not implemented.** It requires multiple API instances behind a load
balancer, which requires moving the in-process rate limiter and dedupe window
to a shared backend. See `docs/LIMITATIONS.md` and
`docs/architecture/ha-design.md`.

Today's upgrade therefore involves a short control-plane outage. **Recording
continues during the API restart** because the recorder is a separate process
— that is the point of ADR-0013. Only the API restart interrupts recording,
and only for the seconds the recorder needs to finalize its open segments.

## Post-upgrade verification

```bash
curl -s localhost:8080/api/v1/system/version
curl -s localhost:8080/readyz | jq
curl -s -H "Authorization: Bearer $TOKEN" \
     localhost:8080/api/v1/recordings/health/summary | jq
```

Confirm the recording camera count matches the pre-upgrade figure, and play
back footage spanning the upgrade window to prove continuity.
