# Preserve foreground capacity during automatic backups

This is an operational override for the existing v0.187 API image, not a source default change.
It sets only MAX_CONCURRENT_BACKUPS_PER_RUNNER=1. Creation throttling remains disabled.
CPU/RAM/disk quotas, normal/non-ephemeral semantics, Runner heavy=3/cleanup=2 and data mounts remain unchanged.

The existing BackupManager counts sandbox backupState=IN_PROGRESS before dispatch under a runner lock.
For v2, a newly queued CREATE_BACKUP job is marked IN_PROGRESS on the sandbox immediately, so queued
and running jobs both consume this API-side limit. Existing jobs are not cancelled when the limit shrinks.
The current Redis lock expires after 10 seconds and has no ownership renewal; this mitigation is not a
hard distributed execution guarantee under lock expiry, multiple dispatchers, or direct Runner calls.

Run prepare only to validate Compose and save private snapshots. It does not recreate containers.
Apply is gated by the final cleanup audit for formal-71b16897, fresh zero active jobs and four ready Runners.
Apply recreates only api, retaining the exact current image/env/mounts except the one added setting.
Failure recreates the original six-file Compose configuration and verifies health.
The script refuses a repeated apply, changed base Compose files, image/tag drift, environment drift,
mount changes, or unrelated configuration differences.
Private plan/inspect/diagnostics must stay outside Git, mode 0600.

Approved execution, only after the running formal test has ended and root has explicitly authorized:
python3 manage_backup_limit.py apply --confirm-production-change --cleanup-proof <final-audit.json>
Rollback after review:
python3 manage_backup_limit.py rollback --confirm-production-change

Offline proof transpiles the exact existing BackupManager methods plus v2 adapter in a no-network
container, without production credentials. It demonstrates default six, candidate one, queued-job
accounting, concurrent normal lock serialization, completion admitting the next backup, and preservation
of an already queued backlog. It does not simulate Redis lock expiry or prove arbitrary distributed fencing.

The separate targeted validation uses eight owned normal apps: actual build/save, destruction and cold
restoration, waits for naturally scheduled CREATE_BACKUP jobs with startedAt, and then creates sixteen
additional normal apps through Deepdiver. A pass needs at least one same-runner foreground CREATE
claimed and completed during a seed BACKUP's execution interval, both jobs ultimately COMPLETED,
all business-flow assertions, and exact owned-resource cleanup. The production warm pool is excluded.
No manual backup, ephemeral switch, task cancellation or concurrency bypass is used to force overlap.
No observed overlap is inconclusive rather than a pass.

Preparation v2 keeps the prior private plan unchanged. It records a canonical hash of the complete rendered base Compose configuration, including env_file and interpolation. Every apply and rollback re-renders and rejects drift before recreating the API. Rendered API environment values merged over immutable image default ENV must exactly match the saved running container environment. Unsupported unresolved/null environment values are rejected. Full rendered config and plans remain private and are never committed.

Explicit natural pool rotation reconciliation: the original final audit remains unchanged and keeps protectedWarm80Passed=false/cleanupPassed=false when pool identities naturally expire. Pass --warm-reconciliation /private/evidence/warm-rotation-reconciled.json only after reviewing the independent TTL/source/replacement proof. The shared maintenance_reconciliation.py verifies run/manifest/owner/per-app cleanup, 400 CREATE and DESTROY completions, evidence hashes, 80 retirement ages, exact fresh replacement identities, snapshot/profile/skill/baseline fingerprints, and cancelled backups after destruction. Default behavior without the flag retains the original strict rejection. Both manager and API wrapper enforce the same sidecar, and the manager freshly rechecks all live warm identities and metadata before applying. The evidence bundle, actual plans, manifests, logs, and current IDs remain private outside Git. These hashes check integrity; they are not cryptographic signatures.
