# Complete Runner build

Runner requires both daemon-amd64 and daytona-computer-use in its embedded filesystem. The daemon also embeds terminal HTML, xterm.js 5.3.0, xterm.css, the 0.8.0 fit addon, the recording dashboard, matplotlib_wrapper.py and repl_worker.py. The HTML and Python resources are tracked source files; daemon:prepare downloads the three pinned terminal assets.

Use VERSION=<reviewed-version> bash hack/runner/build-complete-amd64.sh in an isolated copy with Go and the computer-use X11 build libraries. This runs the dependency steps from apps/runner/project.json, builds an Alpine-compatible CGO_ENABLED=0 Runner and daemon, validates the two embedded ELF files, and emits dist/runner-assets.sha256. Computer-use retains its required CGO/X11 support. Do not replace the complete build with go build on Runner alone.

Before publishing or rolling production, start the final runtime image with its real entrypoint, dummy credentials, an isolated network and a fresh disposable Docker data volume. Require inner docker info, Runner HTTP health, both extracted assets matching the manifest, zero restarts and no unexpected containers. A --help/config-only check does not validate asset extraction or startup. Never mount an existing Runner's Docker volume for this test.
