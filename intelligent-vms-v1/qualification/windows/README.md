# Windows external qualification harness

Issue #21. Layer A is software/readiness evidence only. It does not qualify Windows 10/11, any camera/vendor/model, capacity, or production signing.

Allowed states: NOT_RUN, BLOCKED_EXTERNAL, PASS, FAIL, PASS_WITH_LIMITATION, NOT_APPLICABLE.

Evidence sources: HOSTED_CI, VM_EXTERNAL, PHYSICAL_MACHINE, REAL_CAMERA, SIMULATED.

The validator rejects PASS/PASS_WITH_LIMITATION for external-only Windows 10/11, real-camera/PTZ/event, real soak/capacity/performance and real-storage test IDs when source is HOSTED_CI or SIMULATED.

Every finalized run is bound to a VMS-WIN-YYYYMMDD-### run ID, exact Git SHA, installer filename/SHA-256, product/server/client versions and DB schema.

Commands use qualification/windows/scripts/qualify.py: new-run, validate, summarize, release-manifest, redact, verify-signature and performance-template.

No harness command reboots, kills services, changes networking, fills disks, uninstalls the product, deletes recordings, or uploads evidence. Disruptive scenarios remain explicit tester actions in an authorized lab.
