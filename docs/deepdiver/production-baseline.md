# Deepdiver production baseline

The active maintenance branch is `codex/dsec-api-v0187-20260929` in
`zjing0329/daytona`; upstream is `daytonaio/daytona`. Both API and Runner
patches are maintained together; the current upgrade target is v0.190.0.
The branch name is retained for remote continuity. See [upgrade-v0190.md](upgrade-v0190.md)
and the historical [unified baseline](unified-baseline.md) for source ancestry.

The retired maintenance branch started from 75769315 (v0.190.0 plus 132899b
lifecycle concurrency and 7576931 unlimited stress settings). Its later node
admission replaces those poller-only gates, and its complete cumulative Runner
patch is preserved in the unified branch. The retired tip remains recoverable
through an archival tag; it is no longer an active development branch.

The compose files under docker/ preserve reviewed non-secret production
controls: external runners, service restart policies, Maildev health checks,
proxy settings, other rate limits and bounded Jaeger memory. The fixed sandbox
create throttle is disabled with empty TTL and limit, matching the later
production decision. Other rate-limit buckets remain configured. Apply the
reviewed API image override and backup-foreground overlay as well; these files
alone are not a complete production deployment.

The preserved DEFAULT_REGION_ENFORCE_QUOTAS=false setting controls the default
for newly created regions; it does not establish whether existing regions or
organization quotas are disabled. Per-container RESOURCE_LIMITS_DISABLED and
node admission are separate controls. Environment and credential files are excluded. Source branches do not establish deployed image
identity: before the v0.190 API rollout, production used API source 3cb5e97 and Runner
source 69bfed6. The Runner build-input projection matches the upgraded unified
source, so the API rollout retains the existing Runner image and its truthful
source label. The private deployment receipt records the actual API image and
revision after rollout. Preserve image digests for rollback.
