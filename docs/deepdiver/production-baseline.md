# Deepdiver production baseline (2026-09-29)

This maintenance branch starts from production source HEAD 75769315 and retains
132899b (lifecycle concurrency) and 7576931 (explicit unlimited stress setting).
The upstream repository is daytonaio/daytona; origin is zjing0329/daytona.
No production containers were changed when recording this baseline.

The compose files under docker/ capture reviewed, non-secret control-plane
overrides: external runners, service restart policy, Maildev healthcheck, proxy
domain and rate limits, and bounded Jaeger storage. docker-compose.live.yaml
is a deployment-specific inventory output; it is not a general development default.
Quota enforcement remains disabled as in the inspected deployment; this is
separate from Runner admission and container resource enforcement.
The original backup files and all environment/credential files are excluded.

The inspected production Runner image was deepdiver/daytona-runner:stability-132899b.
A checkout commit is not proof of an image's exact source or build provenance.
Review future rollout against explicit image digests and preserve a rollback image.
