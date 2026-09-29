# Runner node admission and staged rollout

The maintenance branch adds two node-wide operation budgets. Create, start,
restore/recover, resize, snapshot pull/build/commit and backup share the heavy
budget. Stop, destroy, image removal and pause use a separate finite cleanup
budget. A parent operation carries an internal lease through nested Docker calls,
so create -> start and backup -> image removal do not deadlock or double-count.
A cleanup lease cannot authorize a new heavy operation.

This borrows DSec's separation of admission from execution and resource-aware
backpressure. It does not implement DSec's remote-storage architecture, hot
migration, hardware disaggregation or a new container runtime. Persistent sandbox
CPU/memory enforcement and tenant quotas remain separate controls; the production
RESOURCE_LIMITS_DISABLED setting has not been changed.

## Claim protocol and compatibility

The new API exposes authenticated GET /jobs/admission/capabilities (version 1)
and GET /jobs/admission/poll?class=heavy|cleanup&limit=N. These are separate
routes, not optional arguments on the legacy route. A Runner reserves N available
node permits before asking the API to mark N jobs IN_PROGRESS. Unused permits are
released. The database claim uses UPDATE WHERE status=PENDING, preventing duplicate
claims across concurrent legacy and new requests. Class filters apply before
selection, so cleanup remains claimable while heavy capacity is exhausted.

A new Runner against an old API falls back only after the capability endpoint
returns 404. It reserves both a heavy and a cleanup permit for each unknown job,
then releases the unused class after receipt. This conservative mode never
overclaims but is limited by the smaller budget and cannot prioritize queued
cleanup while heavy pressure blocks polling. Upgrade the API first for independent
cleanup polling. Authentication and transient server errors never trigger downgrade.

Old Runners continue using the unchanged legacy endpoint. They benefit from the
conditional claim update but do not acquire the new node budgets until upgraded.
If upgrading the API after a new Runner has selected legacy mode, restart that
Runner during the planned rollout to renegotiate capabilities.

Existing IN_PROGRESS jobs are read in pages before execution. One bounded producer
per class reacquires the same node permits. Cleanup recovery and polling progress
independently while heavy recovery drains. There is no goroutine per waiting job.
A malformed API response with too many jobs or the wrong class is not executed
outside its reservation; errors are logged and existing stale-job reconciliation
remains responsible for unexecuted IN_PROGRESS rows.

Direct HTTP lifecycle calls use the same Docker boundary admission. They return
RUNNER_BUSY / HTTP 429 when no permit is available. Asynchronous pull/build/backup
reserve before accepting work and hold the permit until the actual background
operation finishes. Clients must retry 429 with backoff. An accepted operation
is not canceled merely because its HTTP response has completed.

## Configuration

See docker/runner-admission.env.example. Defaults are 3 heavy and 2 cleanup
operations. NODE_HEAVY_CONCURRENCY and NODE_CLEANUP_CONCURRENCY replace the earlier
poller-only SANDBOX_CREATE_CONCURRENCY and SANDBOX_DESTROY_CONCURRENCY gates.
Legacy variables still parse for compatibility but no longer control admission.
Zero/unlimited node concurrency is intentionally not supported.

NODE_PRESSURE_ENABLED defaults to false. Enable it on a drained canary only after
checking the measurement paths inside its Runner container:

- NODE_PRESSURE_PROC_ROOT must expose the intended host's meminfo and pressure
  files. MemAvailable, not MemFree, is measured.
- NODE_PRESSURE_DISK_PATH must be on the same filesystem as DockerRootDir. Do not
  accidentally measure the Runner container's overlay root. For an external Docker
  daemon, mount its data filesystem read-only at an explicit measurement path.
- Memory available, disk available and inode available percentages set minimum
  headroom; IO PSI some avg10 sets maximum stall pressure. CPU PSI is optional and
  disabled by default because CPU contention alone is not proof of unsafe load.
- A threshold of 0 disables that measurement. Enabled measurements fail closed if
  unavailable or malformed; cleanup always remains available. Reopening requires
  20% headroom beyond the configured threshold to avoid oscillation.
- These measurements describe the selected node/filesystem, not each sandbox's
  cgroup. No CPU allocation-sum assumption or new CPU quota is introduced.

Prometheus exports daytona_runner_admission_capacity,
daytona_runner_admission_reserved, daytona_runner_admission_denied_total,
daytona_runner_admission_pressure and daytona_runner_admission_pressure_blocked.
Reserved counts include in-flight poll requests, not only executing Docker work.
Dynamic pressure pauses new heavy admission without interrupting existing work.

## Validation and rollout

The production host has no Nix installation. Validation uses its existing
deepdiver/daytona-runner-builder:stability-132899b image for Go 1.25.5, a restricted
Runner/common/API-client go.work, and the installed API image's Node/TypeScript
runtime. Test containers have CPU/memory limits, no production Docker socket,
no production volumes or credentials. API CAS integration uses a disposable
PostgreSQL container with no published ports.

Run the Go admission, poller, Docker and controller packages with -race; compile
cmd/runner. The node:test file job-admission.node-test.cjs uses installed ts-node
and can optionally run PostgreSQL integration with DSEC_TEST_DATABASE pointing
only to a dedicated test database. No load or lifecycle tests were run against
production sandboxes.

Stage API before Runner. Drain and upgrade one Runner with conservative static
budgets first; compare queue waiting time, lifecycle p95/p99, failure rate, memory,
IO PSI and disk/inode headroom. Then enable the pressure guard with verified paths.
Gradually expand only after those measurements are stable. Keep cleanup capacity
finite and nonzero. Increase heavy concurrency based on actual IO capacity rather
than the count of declared sandbox CPU cores.

Rollback pressure by setting NODE_PRESSURE_ENABLED=false in the planned rollout.
Rollback static admission with the saved prior Runner image, and retain the old API
endpoint throughout. These commands/configuration have not been applied to live
containers. The fork commits are local to the server and have not been pushed.


## Recovery freshness and stale-job arbitration

After waiting for its node permit, each recovered job is revalidated before
execution. APIs advertising recoveryRenewal=true support POST
/jobs/admission/recover/:jobId: it conditionally renews updatedAt only for the
same Runner's IN_PROGRESS row and observed version. Terminal and wrong-owner
jobs are never revived. The stale scanner also compares status, observed
version and its timeout predicate, so a scan taken before successful renewal
cannot fail that renewed job.

Older APIs, including admission version 1 without that capability flag, receive
a fresh GET /jobs/:jobId after capacity is obtained. Non-IN_PROGRESS jobs are
skipped; authentication and server errors do not downgrade to blind execution.
The legacy GET cannot atomically renew the stale timer, so a state change between
that read and execution remains possible. Upgrade the API before rollout to gain
renewal arbitration.

This is a bounded startup recovery safeguard, not durable fencing or ongoing
job heartbeats. Existing per-job execution stale timeouts still apply after
renewal. Revalidation errors leave work for existing reconciliation rather than
executing a stale cached payload. No unbounded recovery goroutines are introduced.
