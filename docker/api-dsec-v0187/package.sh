#!/bin/sh
set -eu
source_dir=$(git rev-parse --show-toplevel)
build_dir=${DSEC_API_BUILD_DIR:?Set DSEC_API_BUILD_DIR to the completed build output}
context_dir="$build_dir/runtime-context"
mkdir -p "$context_dir/dist/apps/api" "$context_dir/apps/api/src/sandbox/controllers" "$context_dir/apps/api/src/sandbox/services"
for name in main.js main.js.map 3rdpartylicenses.txt; do
  cp "$build_dir/workspace/dist/apps/api/$name" "$context_dir/dist/apps/api/$name"
done
cp "$build_dir/workspace/apps/api/src/sandbox/controllers/job.controller.ts" "$context_dir/apps/api/src/sandbox/controllers/"
cp "$build_dir/workspace/apps/api/src/sandbox/services/job.service.ts" "$build_dir/workspace/apps/api/src/sandbox/services/job-admission.ts" "$context_dir/apps/api/src/sandbox/services/"
for relative in common/errors/runner-delete-maintenance.error.ts sandbox/repositories/sandbox.repository.ts sandbox/services/sandbox.service.ts filters/all-exceptions.filter.ts; do
  mkdir -p "$context_dir/apps/api/src/$(dirname "$relative")"
  cp "$build_dir/workspace/apps/api/src/$relative" "$context_dir/apps/api/src/$relative"
done
source_revision=$(git rev-parse HEAD)
docker build --network=none --pull=false   -f "$source_dir/docker/api-dsec-v0187/Dockerfile.runtime"   --build-arg "SOURCE_REVISION=$source_revision"   -t "${DSEC_API_IMAGE_TAG:-deepdiver/daytona-api:v0.187.0-dsec-candidate}" "$context_dir"
