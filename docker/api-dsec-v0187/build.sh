#!/bin/sh
# Build only the API backport; retain the deployed v0.187.0 runtime dependencies.
set -eu
# This overlay preserves the v0.187 runtime closure. Full upgrades use api-dsec-v0190.
if ! git diff --quiet 8a446cb96331737e5a2118cbcaa0604d95c07f71 -- package.json yarn.lock apps/api/src/migrations; then
    echo "Refusing the v0.187 overlay recipe on a changed dependency/schema baseline. Use docker/api-dsec-v0190." >&2
    exit 1
fi
base_image=daytonaio/daytona-api@sha256:8de6315a378430a58a44ce6c20b41050c2f602446e75f3ff559edbaa0b3758a7
source_dir=$(git rev-parse --show-toplevel)
build_dir=${DSEC_API_BUILD_DIR:?Set DSEC_API_BUILD_DIR to an isolated absolute output directory}
mkdir -p "$build_dir"
docker run --rm --name daytona-dsec-api-builder-20260929 --cpus 4 --memory 8g   --entrypoint sh -e CI=true -e NX_DAEMON=false -e NX_SKIP_NX_CACHE=true   -e PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 -e PUPPETEER_SKIP_DOWNLOAD=1   -v "$source_dir:/source:ro" -v "$build_dir:/build" "$base_image" -c '
set -eu
apt-get update
apt-get install -y --no-install-recommends bash python3 python3-setuptools make g++ git
mkdir -p /build/workspace
cp -a /source/. /build/workspace/
cd /build/workspace
yarn install --immutable
yarn tsc --noEmit -p apps/api/tsconfig.app.json
NODE_ENV=production yarn nx build api --configuration=production --nxBail=true --skip-nx-cache
'

sh "$source_dir/docker/api-dsec-v0187/package.sh"
