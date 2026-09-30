# Daytona v0.190 upgrade

The active source branch remains codex/dsec-api-v0187-20260929; its name is
historical, and its current build target is v0.190.0. No second maintenance
branch is introduced.

## Source and scope

The complete upstream source delta from v0.187.0
(8a446cb96331737e5a2118cbcaa0604d95c07f71) to v0.190.0
(01c502bb1f1ff8f2885d0cd490e043736083dca8) is applied on consolidated
c5d341052db573e6c864d784150fe63d3d2c3282. The API DELETE maintenance guard,
node admission, recovery renewal/revalidation and page size 100 are retained.
PAUSE is included in the API cleanup class to agree with Runner classification;
existing Docker sandboxes still reject pause.

Upstream fixes remove a job-completion unlock that could delete a running
state-sync loop's lock, reject lost action locks, and await sandbox/snapshot
sync work before releasing its lock. These address duplicate job and
state-transition races. SDKs, API DTOs/entities, dashboard and locked dependency
versions are updated together. Nineteen upstream trailing-whitespace additions
are normalized without changing behavior.

The pressure guard remains opt-in. This upgrade does not introduce dynamic
stage concurrency or DSec's storage architecture. Existing create throttler
opt-out, backup limit 1, node heavy/cleanup budgets and quotas stay unchanged.

## Images and Runner preservation

A full API image must contain the v0.190 runtime dependency closure, dashboard,
all API source/entities/migrations and migration toolchain. The older
docker/api-dsec-v0187 seven-file overlay is incompatible with this upgrade;
its build scripts reject this source baseline. Use docker/api-dsec-v0190/.

The deployed Runner image retains source
69bfed6ea8fc4f663532c18dd4fc9a06d8098983. Its complete runtime source and
workspace dependency projection matches the upgraded unified tree. Keep that
image and label instead of restarting identical Runner code. Preserve fresh
exact sandbox/container ID evidence before and after the API rollout.

## Migration and rollback

Four pre-deploy migrations add pause/resume enum values, update default
SUPER_ADMIN permissions, add usage regionType columns and add domainAllowList.
Test execution against a restored production backup in an isolated network,
and validate representative data and row counts. Store backups and private
fixture data only on the server with restricted permissions.

Use a short DDL lock timeout and record the exact applied migrations. Keep
RUN_MIGRATIONS behavior explicit so candidate startup cannot unexpectedly
perform an unreviewed migration phase.

Rollback is an image rollback on the expanded schema after proving old API
compatibility. Do not automatically run migration down: enum rollback is empty,
permission rollback overwrites a fixed array, and column drops discard new data.
Reject rollback with pause/resume states or populated domainAllowList values
that the old API cannot interpret. Preserve original role permissions for an
exact reviewed restoration if needed. Production database restore is separately
coordinated recovery, not the default rollout failure handler.

## Compatibility checks

Validate admission/recovery, DELETE maintenance, lock-race regressions,
CORS/dashboard/OIDC, migration idempotence and old-image compatibility.
The v0.190 CORS policy removes credentialed CORS; authenticated token calls and
the deployed same-origin dashboard must remain functional.
Do not advertise domainAllowList as Docker-enforced isolation: the current
Docker Runner does not implement that new field.

After API health and Runner checks pass, run a small automatic Deepdiver backend
canary with simulated model responses but real sandbox/build/snapshot/cold
restore operations. Scope cleanup to exact test IDs; preserve existing
sandboxes and failed-attempt evidence.

## Validation before rollout

The full candidate image passed 22 admission, maintenance and concurrency tests
with the v0.190 locked dependencies, plus API TypeScript checking. The eight
new concurrency regressions fail against v0.187 code using the same dependencies.

A restored PostgreSQL 18 production backup passed 109-to-113 migration,
existing-column data digests for sandbox/job/both usage tables, API admission
and recovery HTTP checks, dashboard assets, bearer CORS and Dex discovery.
The default SUPER_ADMIN migration results in 15 permissions. The old v0.187
image also starts and passes the HTTP checks on the expanded schema.
A continuously held table lock proves the migration connection honors the
2-second lock timeout through PGOPTIONS; the failed attempt leaves 109
migration records and no new domain column. Isolated fixtures were removed.

## Evidence

Private evidence lives on a1 under
/opt/codex-dsec-20260930/upgrade-v0190/ and the app canary on b1 under
/home/szm/codex-dsec-20260930/upgrade-v0190-canary/.
A source commit or this document is not proof of deployment. The final rollout
receipt identifies the actual image, migrations, health observations, rollback
material and post-deploy canary outcome.
