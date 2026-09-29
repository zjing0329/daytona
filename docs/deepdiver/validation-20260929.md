# Validation on the production-side isolated worktree

Date: 2026-09-29. Host: 192.168.0.106.
Worktree: /opt/codex-dsec-20260929/daytona.
Branch: codex/dsec-maintenance-20260929. Origin: zjing0329/daytona.
No push, live container update, live Docker lifecycle test or data migration.

## Go

The complete apps/runner/... tree passes go test -race -count=1.
There are 23 top-level test functions across the source tree, including the
existing SSH gateway tests. New tests cover shared capacity, nested leases,
cleanup isolation, pressure/hysteresis, malformed PSI, cancellation, claim
limits/failures, old API fallback, bounded recovery, asynchronous HTTP rejection
and backup lease lifetime.

Go 1.25.5 came from the existing builder image
sha256:d3a42a6f5f831747c433cdeb8af65c017d0dcfd0f2fd875a0fed6afefb183b9e.
Nix is absent on the server; the test uses a temporary go.work containing only
apps/runner, libs/common-go and libs/api-client-go. Dependency versions are not
changed. The container is limited to 4 CPUs and 8 GiB RAM, with no production
Docker socket or credential mounts. Embedded daemon/computer-use assets come
from that builder image; no new daemon/computer-use build is claimed.

Candidate Runner binary: /opt/codex-dsec-20260929/daytona-build/daytona-runner.
SHA256: aa0f263dd609c4aaad7179396e617854b222ea66d1e4442190d1e661a2fea418.
It has been built but not installed or executed against production.

## API

Five Node tests pass, including real PostgreSQL integration: 10 concurrent
claims return a pending job once, cleanup selection excludes heavy jobs, and
another Runner's rows remain unclaimed. The test database uses an isolated
container with no published ports and has been removed after validation.

Full API TypeScript checking reports 23 diagnostics in BOTH the untouched
bffe5d2 baseline and the modified tree, with exactly identical diagnostic sets.
They concern existing Express Request.user declarations and missing Jest types
in the installed production API image. No new TypeScript diagnostics appear.
This is not a claim that a clean full API typecheck or API image build passed.

## Deployment baseline and limits

The four recorded compose files pass combined docker compose config validation
with interpolation/environment-file resolution disabled; no credentials are
needed. git diff --check passes.

No production throughput benchmark, actual sandbox lifecycle smoke test, MinIO
load test or live canary rollout was performed. The new API and Runner must be
rolled out in the order described in runner-admission.md, with existing image
digests retained for rollback. Dynamic pressure gating remains opt-in.
