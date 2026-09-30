#!/usr/bin/env python3
"""Bind one complete build's inputs, toolchain and outputs for packaging."""
import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess

ASSETS = (
    "dist/apps/runner-amd64", "dist/apps/daemon-amd64", "dist/libs/computer-use-amd64",
    "apps/daemon/pkg/terminal/static/xterm.js", "apps/daemon/pkg/terminal/static/xterm.css",
    "apps/daemon/pkg/terminal/static/xterm-addon-fit.js",
)
BUILD_ROOTS = ("apps/runner", "apps/daemon", "libs/common-go", "libs/api-client-go",
               "libs/computer-use", "libs/toolbox-api-client-go")
GENERATED = set(ASSETS) | {"apps/runner/pkg/daemon/static/daemon-amd64",
    "apps/runner/pkg/daemon/static/daytona-computer-use"}
RECIPES = ("hack/runner/build-complete-amd64.sh", "hack/runner/build_receipt.py")


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else hashlib.sha256(stream.read()).hexdigest()


def input_hashes(root):
    names = {"go.work", *RECIPES}
    # go.work.sum is a generated workspace checksum cache in this repository.
    # Record it separately after build; dependency versions stay in module manifests.
    for directory in BUILD_ROOTS:
        for path in (root / directory).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                names.add(path.relative_to(root).as_posix())
    # Workspace module manifests affect Go's minimum-version selection, even
    # when their packages are not imported by Runner.
    for directory in re.findall(r"\./([A-Za-z0-9_./-]+)", (root / "go.work").read_text()):
        if ".." in pathlib.PurePosixPath(directory).parts:
            raise ValueError("Invalid workspace module path")
        for name in ("go.mod", "go.sum"):
            path = root / directory / name
            if path.is_file():
                names.add(path.relative_to(root).as_posix())
    return {name: sha(root / name) for name in sorted(names - GENERATED)}


def fingerprint(inputs):
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def begin(root, revision, version):
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or not version:
        raise ValueError("Set reviewed SOURCE_REVISION and VERSION")
    toolchain = json.loads(subprocess.check_output(["go", "env", "-json", "GOOS", "GOARCH", "GOWORK", "GOTOOLCHAIN", "GOFLAGS"], text=True))
    if pathlib.Path(toolchain["GOWORK"]).resolve() != root / "go.work":
        raise ValueError("Complete build must use this checkout's go.work")
    toolchain["version"] = subprocess.check_output(["go", "version"], text=True).strip()
    inputs = input_hashes(root)
    # A receipt must never attest pre-existing output files from another build.
    for name in GENERATED | {"dist/runner-assets.sha256", "dist/build-receipt.json", "dist/build-start.json"}:
        (root / name).unlink(missing_ok=True)
    (root / "dist").mkdir(exist_ok=True)
    receipt = dict(source_revision=revision, version=version, workspace_root=str(root),
        inputs=inputs, input_fingerprint=fingerprint(inputs), toolchain=toolchain,
        cgo={"runner": False, "daemon": False, "computer_use": True})
    (root / "dist/build-start.json").write_text(json.dumps(receipt, indent=2) + "\n")


def finish(root, revision, version):
    receipt = json.loads((root / "dist/build-start.json").read_text())
    if receipt["source_revision"] != revision or receipt["version"] != version or receipt["inputs"] != input_hashes(root):
        raise ValueError("Build inputs changed while compiling")
    receipt["outputs"] = {name: sha(root / name) for name in ASSETS}
    checksum = root / "go.work.sum"
    receipt["workspace_checksum_sha256"] = sha(checksum) if checksum.is_file() else None
    receipt["complete"] = True
    (root / "dist/build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


def validate(root, revision, assets):
    receipt = json.loads((root / "dist/build-receipt.json").read_text())
    inputs = input_hashes(root)
    if receipt.get("complete") is not True or receipt.get("source_revision") != revision:
        raise ValueError("Receipt revision or completion mismatch")
    if receipt.get("inputs") != inputs or receipt.get("input_fingerprint") != fingerprint(inputs):
        raise ValueError("Receipt build inputs mismatch")
    checksum = root / "go.work.sum"
    if receipt.get("workspace_checksum_sha256") != (sha(checksum) if checksum.is_file() else None):
        raise ValueError("Generated workspace checksum mismatch")
    if receipt.get("outputs") != assets:
        raise ValueError("Receipt build outputs mismatch")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("begin", "finish"))
    parser.add_argument("--root", type=pathlib.Path, required=True)
    args = parser.parse_args()
    globals()[args.action](args.root.resolve(), os.environ.get("SOURCE_REVISION", ""), os.environ.get("VERSION", ""))
