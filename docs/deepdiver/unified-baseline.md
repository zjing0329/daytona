# Unified v0.187 source baseline

The active branch is `codex/dsec-api-v0187-20260929` in `zjing0329/daytona`.
API and Runner are built from the same source commit, using separate build
recipes and images. This consolidation does not deploy either image.

## Source provenance

- Base: v0.187.0, `8a446cb96331737e5a2118cbcaa0604d95c07f71`.
- Existing API/operations parent: `8070aa6e6b7b2d7830f7788600695f6fb6ff8659`.
- Imported Runner patch: cumulative `01c502bb1f1ff8f2885d0cd490e043736083dca8..69bfed6ea8fc4f663532c18dd4fc9a06d8098983`,
  restricted to its 25 changed `apps/runner/` files. This includes the earlier
  132899b/7576931 lifecycle fixes and later node admission, recovery freshness,
  pagination and complete embedded-asset test.
- Imported complete-build tools: `hack/runner/` and
  `docker/runner-admission.env.example` from that same reviewed tip. The unified
  build additionally records input/output provenance and validates it during
  image packaging; old hard-coded revision and binary labels are removed.
- API maintenance helpers already exist byte-for-byte under
  `docker/api-dsec-v0187/`; no second `hack/api/` copy is introduced.
- Reviewed production compose controls are preserved. The create bucket's TTL
  and limit are empty, matching its production opt-out; other throttlers remain.
  The separate backup-foreground overlay remains the only backup-limit override.
- Retired branch tip: `69bfed6ea8fc4f663532c18dd4fc9a06d8098983`, preserved at tag
  `codex/dsec-maintenance-20260929-archive-20260930` before branch deletion.

This is a backport, not a merge of v0.190 history. The API source, schema,
migration files, generated SDKs, Go dependencies and shared libraries retain
the existing v0.187 baseline. Runner patch files do not overlap the upstream
v0.187-to-v0.190 Runner changes.

## Building

Use `docker/api-dsec-v0187/build.sh` with an isolated `DSEC_API_BUILD_DIR`
and a unique `DSEC_API_IMAGE_TAG`. It type-checks and builds the API against
the unchanged lockfile, then packages the exact production base.

Use `SOURCE_REVISION=<commit> VERSION=<reviewed-version> bash hack/runner/build-complete-amd64.sh`
inside an isolated Linux Go/Python 3/X11 build environment. It builds daemon, terminal
assets, computer-use and Runner, and writes `dist/runner-assets.sha256`.
Package that complete Runner build with
`hack/runner/package-complete-image.py` (see `hack/runner/README.md` for arguments),
which validates the build receipt and uses
`docker/api-dsec-v0187/Dockerfile.runner`. Then run
`hack/runner/smoke-complete-image.py` using the final image and manifest.
Both build contexts must record the same Git commit. The small packaging
Dockerfile is not a standalone Runner build recipe.

Use the repository Nix shells where available. The production-side host has
no Nix/Go/Node installation, so this verification uses isolated, resource-limited
cached builder containers. Tests do not mount production Docker data or use
production credentials. The entrypoint smoke requires a separate privileged
DIND container with its own disposable volume and isolated network.

## Production and upgrade boundary

Production remains API source `3cb5e97` and Runner source `69bfed6` after
this source-only consolidation. The unified Runner candidate is based on
v0.187 and is not a byte-equivalent rebuild of the current v0.190-based Runner.

Upstream changes deliberately left for a later upgrade include v0.190 SSH
connection cleanup/half-close handling, shared proxy timeouts/buffering, daemon
server timeouts and route compatibility, PAUSE support, newer DTOs and SDKs.
Do not deploy the unified candidate over the current Runner merely because
unit tests pass. Review these differences and the API/Runner compatibility,
then choose and validate the upgrade target with canary and rollback evidence.

The pressure gate is still opt-in and is not a load-adaptive concurrency
controller. Branch unification does not remove fixed stage budgets, increase
production concurrency, or establish new throughput results.

## Upgrade assessment

Evaluate the v0.190 job-state-handler lock fix first: it removes an
unconditional resource-lock deletion after job completion that could remove a
still-running state-sync loop's lock and fan out duplicate jobs. Review the
owner/version semantics and race tests before backporting it. This consolidation
does not apply that additional functional change.

A full v0.187-to-v0.190 API upgrade has four database migrations to rehearse on a
copy: pause/resume enum additions have an empty down migration; the SUPER_ADMIN
permission rollback writes a fixed old permission array; regionType usage
columns and domainAllowList can be dropped but their new data would be lost.
Do not equate a down method with a lossless rollback. PAUSE currently targets
Linux VM/Windows, not the Docker sandboxes used here, so it is not by itself a
reason to upgrade for startup throughput.

## Validation records

The dated consolidation evidence is stored on the server under
`/opt/codex-dsec-20260930/unified-baseline/`. It includes API source/dependency
equality, full API build and isolated protocol tests, Runner race tests and
complete-image smoke, final revision/image manifests and remote-ref checks.
Only completed checks recorded there are evidence of this candidate.
`validation-20260929.md` is explicitly historical and describes earlier
artifacts, including one incomplete Runner that must not be deployed.
