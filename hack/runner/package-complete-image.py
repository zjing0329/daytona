#!/usr/bin/env python3
"""Package verified complete build outputs without deploying a Runner."""
import argparse
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import uuid
import build_receipt

ASSETS = (
    "dist/apps/runner-amd64", "dist/apps/daemon-amd64", "dist/libs/computer-use-amd64",
    "apps/daemon/pkg/terminal/static/xterm.js", "apps/daemon/pkg/terminal/static/xterm.css",
    "apps/daemon/pkg/terminal/static/xterm-addon-fit.js",
)
BUILD_ROOTS = ("apps/runner", "apps/daemon", "libs/common-go", "libs/api-client-go",
               "libs/computer-use", "libs/toolbox-api-client-go", "go.work", "go.work.sum")
CONFIG_KEYS = ("Entrypoint", "Cmd", "Env", "Volumes", "WorkingDir", "User", "Healthcheck", "ExposedPorts")


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else hashlib.sha256(stream.read()).hexdigest()


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def verify_assets(workspace):
    values = {}
    for line in (workspace / "dist/runner-assets.sha256").read_text().splitlines():
        digest, name = line.split(None, 1)
        name = name.strip()
        if name not in ASSETS or name in values or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Unexpected or duplicate manifest entry")
        if sha(workspace / name) != digest:
            raise ValueError("Asset hash mismatch: " + name)
        values[name] = digest
    if set(values) != set(ASSETS):
        raise ValueError("Incomplete build manifest")
    for name in ASSETS[:3]:
        with (workspace / name).open("rb") as f:
            if f.read(4) != b"\x7fELF":
                raise ValueError("Missing ELF resource: " + name)
    return values


def verify_source(source, workspace, revision):
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or run("git", "-C", str(source), "rev-parse", "HEAD") != revision:
        raise ValueError("Source revision mismatch")
    if run("git", "-C", str(source), "status", "--porcelain"):
        raise ValueError("Source checkout must be clean")
    names = run("git", "-C", str(source), "ls-files", "--", *BUILD_ROOTS, "*.go", "go.mod", "go.sum").splitlines()
    for name in names:
        if not (workspace / name).is_file() or sha(source / name) != sha(workspace / name):
            raise ValueError("Build source differs from commit: " + name)
    source_inputs = build_receipt.input_hashes(source)
    if source_inputs != build_receipt.input_hashes(workspace):
        raise ValueError("Unexpected or changed build input in workspace")
    tracked = set(run("git", "-C", str(source), "ls-files").splitlines())
    if not set(source_inputs).issubset(tracked):
        raise ValueError("Unexpected untracked source input")
    return len(source_inputs)


def runtime_matches_base(base, image):
    layers = base.get("RootFS", {}).get("Layers", [])
    return bool(layers) and image.get("RootFS", {}).get("Layers", [])[:len(layers)] == layers and all(
        image["Config"].get(key) == base["Config"].get(key) for key in CONFIG_KEYS)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-root", type=pathlib.Path, required=True)
    p.add_argument("--workspace", type=pathlib.Path, required=True)
    p.add_argument("--source-revision", required=True)
    p.add_argument("--base-image", required=True)
    p.add_argument("--expected-base-image-id", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--output", type=pathlib.Path, required=True)
    a = p.parse_args()
    if a.output.exists():
        raise ValueError("Preserve existing packaging evidence; choose a new output")
    if subprocess.run(["docker", "image", "inspect", a.tag], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise ValueError("Candidate tag already exists; choose a new tag")
    source_count = verify_source(a.source_root, a.workspace, a.source_revision)
    assets = verify_assets(a.workspace)
    receipt = build_receipt.validate(a.workspace, a.source_revision, assets)
    base = json.loads(run("docker", "image", "inspect", a.base_image))[0]
    if base["Id"] != a.expected_base_image_id or not re.fullmatch(r"sha256:[0-9a-f]{64}", a.expected_base_image_id):
        raise ValueError("Runtime base image mismatch")
    # BuildKit treats bare sha256 image IDs as registry names. Give the verified
    # local ID a unique temporary reference, then verify both identity and layers.
    reference = "deepdiver/runner-build-base:" + uuid.uuid4().hex
    run("docker", "image", "tag", base["Id"], reference)
    try:
        with tempfile.TemporaryDirectory(prefix="complete-runner-") as directory:
            context = pathlib.Path(directory)
            shutil.copy2(a.workspace / ASSETS[0], context / "daytona-runner")
            shutil.copy2(a.source_root / "docker/api-dsec-v0187/Dockerfile.runner", context / "Dockerfile")
            subprocess.run(["docker", "build", "--network=none", "--pull=false", "-t", a.tag,
                "--build-arg", "BASE_IMAGE=" + reference,
                "--build-arg", "BASE_IMAGE_ID=" + base["Id"],
                "--build-arg", "SOURCE_REVISION=" + a.source_revision,
                "--build-arg", "RUNNER_BINARY_SHA256=" + assets[ASSETS[0]], str(context)], check=True)
        if json.loads(run("docker", "image", "inspect", reference))[0]["Id"] != base["Id"]:
            raise ValueError("Temporary runtime base reference changed")
    finally:
        subprocess.run(["docker", "image", "rm", reference], check=False, stdout=subprocess.DEVNULL)
    image = json.loads(run("docker", "image", "inspect", a.tag))[0]
    if not runtime_matches_base(base, image):
        raise ValueError("Runtime base layers or configuration changed")
    labels = image["Config"].get("Labels", {})
    if labels.get("org.opencontainers.image.revision") != a.source_revision or labels.get("io.deepdiver.runner.binary-sha256") != assets[ASSETS[0]] or labels.get("io.deepdiver.base.image-id") != base["Id"]:
        raise ValueError("Image provenance mismatch")
    result = dict(source_revision=a.source_revision, source_files_verified=source_count,
        assets_sha256=assets, base_image_id=base["Id"], image=a.tag, image_id=image["Id"],
        runtime_config_preserved=True, requires_entrypoint_smoke=True,
        build_input_fingerprint=receipt["input_fingerprint"], build_toolchain=receipt["toolchain"],
        build_receipt_sha256=sha(a.workspace / "dist/build-receipt.json"))
    a.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
