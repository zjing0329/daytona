#!/usr/bin/env bash
# Run inside a Go/Linux build environment with the computer-use X11 dependencies.
# This follows runner/project.json's dependencies; never compile Runner alone.
set -euo pipefail
source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$source_root"
: "${VERSION:?Set VERSION to the reviewed Runner/daemon release version}"
: "${SOURCE_REVISION:?Set SOURCE_REVISION to the reviewed source commit}"
export GOOS=linux GOARCH=amd64
python3 hack/runner/build_receipt.py begin --root "$source_root"
mkdir -p dist/apps dist/libs apps/runner/pkg/daemon/static
# daemon:prepare provides the terminal assets embedded by the daemon.
go run apps/daemon/tools/xterm.go
for asset in xterm.js xterm.css xterm-addon-fit.js; do
    test -s "apps/daemon/pkg/terminal/static/$asset"
done
# Refuse an HTTP error body accidentally downloaded as a JavaScript asset.
grep -q "Terminal" apps/daemon/pkg/terminal/static/xterm.js
grep -q ".xterm" apps/daemon/pkg/terminal/static/xterm.css
grep -q "FitAddon" apps/daemon/pkg/terminal/static/xterm-addon-fit.js
CGO_ENABLED=0 go build -trimpath \
    -ldflags="-X github.com/daytonaio/daemon/internal.Version=$VERSION" \
    -o dist/apps/daemon-amd64 ./apps/daemon/cmd/daemon
# computer-use needs CGO/X11 in the sandbox; the Runner and daemon do not.
(
    cd libs/computer-use
    CGO_ENABLED=1 GONOSUMDB=github.com/daytonaio/daytona go build -trimpath \
        -o ../../dist/libs/computer-use-amd64 main.go
)
cp dist/apps/daemon-amd64 apps/runner/pkg/daemon/static/daemon-amd64
cp dist/libs/computer-use-amd64 apps/runner/pkg/daemon/static/daytona-computer-use
CGO_ENABLED=0 go test -tags runner_embedded_assets ./apps/runner/pkg/daemon -count=1
CGO_ENABLED=0 go build -trimpath \
    -ldflags="-X github.com/daytonaio/runner/internal.Version=$VERSION" \
    -o dist/apps/runner-amd64 ./apps/runner/cmd/runner
sha256sum dist/apps/runner-amd64 dist/apps/daemon-amd64 dist/libs/computer-use-amd64 \
    apps/daemon/pkg/terminal/static/xterm.js \
    apps/daemon/pkg/terminal/static/xterm.css \
    apps/daemon/pkg/terminal/static/xterm-addon-fit.js > dist/runner-assets.sha256

python3 hack/runner/build_receipt.py finish --root "$source_root"
