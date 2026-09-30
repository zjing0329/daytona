# Deepdiver production baseline

The active maintenance branch is `codex/dsec-api-v0187-20260929` in
`zjing0329/daytona`; upstream is `daytonaio/daytona`. Both API and Runner
patches are maintained on the v0.187.0 source baseline. See
[unified baseline](unified-baseline.md) for exact source ancestry and validation.

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
identity: production remains API source 3cb5e97 and Runner source 69bfed6 until a
separate validated rollout. Preserve image digests for rollback.
