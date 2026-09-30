#!/bin/sh
set -eu
[ -n "$DSEC_V0190_BUILD_DIR" ]
[ -n "$DSEC_V0190_IMAGE_TAG" ]
[ -n "$DSEC_V0190_BUILDER" ]
source_dir=$(git rev-parse --show-toplevel)
mkdir -p "$DSEC_V0190_BUILD_DIR"
chmod 700 "$DSEC_V0190_BUILD_DIR"
timeout --signal=INT --kill-after=30s 600 docker pull node:24-slim > "$DSEC_V0190_BUILD_DIR/node-base-pull.log" 2>&1
node_base=$(docker image inspect node:24-slim --format '{{index .RepoDigests 0}}')
export DSEC_V0190_NODE_BASE="$node_base"
python3 "$source_dir/docker/api-dsec-v0190/prepare_context.py" "$source_dir" "$DSEC_V0190_BUILD_DIR"
HTTP_PROXY=$(docker info --format '{{.HTTPProxy}}')
HTTPS_PROXY=$(docker info --format '{{.HTTPSProxy}}')
NO_PROXY=$(docker info --format '{{.NoProxy}}')
export HTTP_PROXY HTTPS_PROXY NO_PROXY
# A dedicated docker-container driver bounds the actual build processes, not just Docker CLI.
timeout --signal=INT --kill-after=30s 600 docker pull moby/buildkit:buildx-stable-1 > "$DSEC_V0190_BUILD_DIR/buildkit-pull.log" 2>&1
buildkit_image=$(docker image inspect moby/buildkit:buildx-stable-1 --format '{{index .RepoDigests 0}}')
docker buildx create --name "$DSEC_V0190_BUILDER" --driver docker-container \
  --driver-opt "image=$buildkit_image,memory=12g,cpu-period=100000,cpu-quota=400000" \
  --driver-opt "env.HTTP_PROXY=$HTTP_PROXY,env.HTTPS_PROXY=$HTTPS_PROXY" \
  > "$DSEC_V0190_BUILD_DIR/buildkit-create.log"
timeout --signal=INT --kill-after=30s 120 docker buildx inspect "$DSEC_V0190_BUILDER" --bootstrap > "$DSEC_V0190_BUILD_DIR/buildkit-bootstrap.log" 2>&1
docker inspect "buildx_buildkit_$DSEC_V0190_BUILDER""0" > "$DSEC_V0190_BUILD_DIR/buildkit-limits.json"
python3 - "$DSEC_V0190_BUILD_DIR/buildkit-limits.json" <<'PY'
import json,sys
c=json.load(open(sys.argv[1]))[0]['HostConfig']
assert c['Memory']==12*1024**3 and c['CpuPeriod']==100000 and c['CpuQuota']==400000,c
PY
# Cache the complete v190 builder for dependency-locked tests. Both targets run the unchanged
# upstream multi-stage recipe; only the two Node FROM digests and offline cloud flag differ.
timeout --signal=INT --kill-after=30s 1800 docker buildx build --builder "$DSEC_V0190_BUILDER" --load --pull=false --progress=plain \
  --target builder --build-arg HTTP_PROXY --build-arg HTTPS_PROXY --build-arg NO_PROXY --build-arg VERSION=v0.190.0-dsec-candidate \
  -f "$DSEC_V0190_BUILD_DIR/Dockerfile.pinned" \
  -t "$DSEC_V0190_IMAGE_TAG-builder" "$DSEC_V0190_BUILD_DIR/context" \
  > "$DSEC_V0190_BUILD_DIR/builder-build.log" 2>&1
timeout --signal=INT --kill-after=30s 1800 docker buildx build --builder "$DSEC_V0190_BUILDER" --load --pull=false --progress=plain \
  --build-arg HTTP_PROXY --build-arg HTTPS_PROXY --build-arg NO_PROXY --build-arg VERSION=v0.190.0-dsec-candidate \
  -f "$DSEC_V0190_BUILD_DIR/Dockerfile.pinned" \
  -t "$DSEC_V0190_IMAGE_TAG" "$DSEC_V0190_BUILD_DIR/context" \
  > "$DSEC_V0190_BUILD_DIR/full-build.log" 2>&1
docker image inspect "$DSEC_V0190_IMAGE_TAG" > "$DSEC_V0190_BUILD_DIR/image-inspect.private.json"
docker run --rm --network none --entrypoint sh "$DSEC_V0190_IMAGE_TAG" \
  -c 'node --version; corepack --version; yarn --version' \
  > "$DSEC_V0190_BUILD_DIR/runtime-tool-versions.log"
