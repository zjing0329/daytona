#!/usr/bin/env python3
"""Reviewed v0.187 -> v0.190 API-only rollout. No schema down or production restore.

prepare reads production and creates a private consistent backup. Restore that exact
backup with verify_backup.py before apply. apply requires an explicit confirmation
mode and a reviewed plan digest. Runners are never stopped or recreated.
"""
import argparse
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request
import uuid

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
API = "daytona-api-1"
DB = "daytona-db-1"
REDIS = "daytona-redis-1"
REVISION_LABEL = "org.opencontainers.image.revision"
EXPECTED = [1781022372014, 1781267138889, 1781597992117, 1781740800000]
TARGETS = {"a2": "http://192.168.0.149:3003", "a3": "http://192.168.0.62:3003",
           "a4": "http://192.168.0.231:3003", "a5": "http://192.168.0.162:3003"}
OVERLAY = "/opt/daytona/docker/docker-compose.dsec-api.yaml"
FILES = [
    "/opt/daytona/docker/docker-compose.yaml",
    "/opt/daytona/docker/docker-compose.live.yaml",
    "/opt/daytona/docker/docker-compose.maildev-health.yaml",
    "/opt/daytona/docker/docker-compose.no-local-runner.yaml",
    OVERLAY,
    "/opt/codex-dsec-20260930/api-create-unthrottled/docker-compose.create-unthrottled.yaml",
    "/opt/codex-dsec-20260930/api-backup-foreground/docker-compose.backup-foreground.yaml",
]
ROLE_ID = "00000000-0000-0000-0000-000000000005"
FRESH_SECONDS = 900


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def timestamp(value):
    value = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
    value = re.sub(r"\.(\d{1,5})(?=[+-]|$)", lambda m: "." + m[1].ljust(6, "0"), value)
    parsed = dt.datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def fresh(value, seconds=FRESH_SECONDS):
    age = (timestamp(now()) - timestamp(value)).total_seconds()
    require(-5 <= age <= seconds, "Evidence is stale or future-dated")


def sha(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def write(path, value):
    path = Path(path)
    require(not path.is_symlink(), "Refusing symlink output")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value if isinstance(value, bytes) else value.encode())
        stream.flush()
        os.fsync(stream.fileno())


def save(path, value):
    write(path, json.dumps(value, indent=2) + "\n")


def private(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_mode & 0o077 == 0,
            "Evidence must be a private regular file")
    return path


def env_map(values):
    return dict(item.split("=", 1) for item in values if "=" in item)


def runtime(container):
    return {key: container[key] for key in ("Id", "Image", "Config", "HostConfig", "Mounts")}


def stable_runners(rows):
    return [{k: row[k] for k in ("id", "name", "apiUrl")} for row in rows]


def migration_timestamps(rows):
    return [int(row["timestamp"]) for row in rows]


def validate_migrations(original, current, complete=False):
    old = migration_timestamps(original)
    actual = migration_timestamps(current)
    require(len(old) == 109 and len(set(old)) == 109, "Expected exactly 109 baseline migrations")
    require(current[:109] == original, "Existing migration history changed")
    extra = actual[109:]
    require(extra == EXPECTED[:len(extra)], "Unexpected or out-of-order upgrade migrations")
    if complete:
        require(extra == EXPECTED and len(actual) == 113, "Upgrade is not exactly four migrations")


def validate_inventory(paths, baseline):
    timestamps = [int(Path(path).name.split("-")[0]) for path in paths]
    old = migration_timestamps(baseline)
    require(sorted(timestamps) == sorted(old + EXPECTED),
            "Candidate full migration inventory is not the baseline plus exactly four")
    pending = sorted(path for path in paths if int(Path(path).name.split("-")[0]) not in old)
    require(pending == ["pre-deploy/" + str(stamp) + "-migration.ts" for stamp in EXPECTED],
            "Pending migrations must be exactly four pre-deploy files; none at startup/post-deploy")


def validate_gates(state, warm=None, runners=None, after=None):
    require(state["activeJobs"] == 0, "Pending or in-progress jobs exist")
    require(state["otherActiveRunners"] == 0, "Unexpected schedulable/ready Runner exists")
    rows = state["runners"]
    require(len(rows) == 4 and {x["name"] for x in rows} == set(TARGETS), "Expected four Runner identities")
    for row in rows:
        require(row["apiUrl"] == TARGETS[row["name"]] and row["state"] == "ready"
                and row["unschedulable"] is False and row["draining"] is False,
                "Runner identity/readiness/maintenance gate failed")
        fresh(row["lastChecked"], 120)
        if after:
            require(timestamp(row["lastChecked"]) >= timestamp(after), "Runner health predates API startup")
    if runners is not None:
        require(stable_runners(rows) == stable_runners(runners), "Runner identity changed")
    sandboxes = state["sandboxes"]
    require(len(sandboxes) == 80 and len({s["id"] for s in sandboxes}) == 80,
            "Expected exactly 80 protected active sandboxes")
    require(all(s["state"] == "started" and s["desiredState"] == "started" for s in sandboxes),
            "Protected sandbox API state changed")
    if warm is not None:
        require(sandboxes == warm, "Protected sandbox IDs, placement or states changed")


def validate_restore(plan, proof, backup_path):
    backup = plan["freshBackup"]
    fresh(plan["preparedAt"])
    fresh(backup["finishedAt"])
    fresh(proof["finishedAt"])
    require(proof.get("status") == "success" and proof.get("productionModified") is False
            and proof.get("cleanupVerified") is True, "Fresh backup restore did not succeed and clean up")
    require(proof["backupSha256"] == backup["sha256"] == sha(private(backup_path).read_bytes()),
            "Fresh backup/proof digest differs")
    require(proof["sourceContainerId"] == backup["sourceContainerId"], "Restore source identity differs")
    require(proof["restored"] == backup["counts"], "Restored counts differ from the prepared backup")
    require(timestamp(proof["finishedAt"]) >= timestamp(backup["finishedAt"]), "Restore predates backup")


def validate_candidate(image, revision):
    require(re.fullmatch(r"[0-9a-f]{40}", revision) is not None, "Invalid source revision")
    require(image["Config"].get("Labels", {}).get(REVISION_LABEL) == revision,
            "Candidate image revision differs from clean HEAD")


def validate_rollback(guard):
    require(guard["newStates"] == 0, "Rollback blocked: paused/pausing/resuming state exists")
    require(guard["domainAllowListUsed"] == 0, "Rollback blocked: domainAllowList is in use")


class Host:
    def __init__(self, state):
        self.state = Path(state)

    def command(self, args, timeout=120, input=None, log=None):
        try:
            result = subprocess.run(args, input=input, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            if log:
                write(self.state / log, (error.stdout or b"") + (error.stderr or b""))
            raise RuntimeError("Command timed out; inspect private evidence") from None
        if log:
            write(self.state / log, result.stdout + result.stderr)
        require(result.returncode == 0, "Command failed; output omitted to protect configuration")
        return result.stdout.decode()

    def inspect(self, name):
        return json.loads(self.command(["docker", "inspect", name]))[0]

    def image(self, name):
        return json.loads(self.command(["docker", "image", "inspect", name]))[0]

    def revision(self):
        require(not self.command(["git", "-C", str(REPO), "status", "--porcelain"]).strip(),
                "Source tree must be clean and committed")
        return self.command(["git", "-C", str(REPO), "rev-parse", "HEAD"]).strip()

    def compose(self, alternate=None):
        args = ["docker", "compose", "-p", "daytona", "--project-directory", "/opt/daytona/docker"]
        for file in FILES:
            args.extend(["-f", str(alternate) if alternate and file == OVERLAY else file])
        return args

    def config(self, alternate=None):
        return json.loads(self.command(self.compose(alternate) + ["config", "--format", "json"]))

    def files(self):
        # Include .env explicitly even if its current values are not interpolated.
        paths = FILES + ["/opt/daytona/docker/.env"]
        return {p: sha(Path(p).read_bytes()) if Path(p).exists() else None for p in paths}

    def sql(self, query):
        container = self.inspect(DB)
        values = env_map(container["Config"].get("Env", []))
        user, database = values.get("POSTGRES_USER", "postgres"), values.get("POSTGRES_DB", "daytona")
        return self.command(["docker", "exec", DB, "psql", "-X", "-v", "ON_ERROR_STOP=1",
                             "-U", user, "-d", database, "-At", "-c", query]).strip()

    def db_identity(self):
        c = self.inspect(DB)
        return {"containerId": c["Id"], "imageId": c["Image"], "mounts": c["Mounts"]}

    def state_snapshot(self):
        # A single read-only repeatable snapshot prevents inconsistent cross-query gates.
        query = """BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT json_build_object(
 'migrations',(SELECT json_agg(x ORDER BY x.id) FROM (SELECT id,timestamp,name FROM migrations) x),
 'counts',json_build_object('migrations',(SELECT count(*) FROM migrations),
   'sandbox_count',(SELECT count(*) FROM sandbox),'job_count',(SELECT count(*) FROM job)),
 'activeJobs',(SELECT count(*) FROM job WHERE lower(status::text) IN ('pending','in_progress')),
 'runners',(SELECT json_agg(x ORDER BY x.name) FROM
   (SELECT id,name,"apiUrl",unschedulable,draining,state,"lastChecked" FROM runner WHERE name IN ('a2','a3','a4','a5')) x),
 'otherActiveRunners',(SELECT count(*) FROM runner WHERE name NOT IN ('a2','a3','a4','a5')
   AND (unschedulable IS DISTINCT FROM true OR draining IS DISTINCT FROM false OR state::text='ready')),
 'sandboxes',(SELECT coalesce(json_agg(x ORDER BY x.id),'[]'::json) FROM
   (SELECT id,state,"desiredState","runnerId" FROM sandbox WHERE state::text <> 'destroyed') x),
 'superAdminPermissions',(SELECT permissions FROM organization_role WHERE id='""" + ROLE_ID + """'));
COMMIT;"""
        lines = self.sql(query).splitlines()
        return json.loads("\n".join(line for line in lines if line not in ("BEGIN", "COMMIT")))

    def leases(self, rows):
        for row in rows:
            value = self.command(["docker", "exec", REDIS, "redis-cli", "--raw", "PTTL",
                                  "runner:maintenance:delete:" + row["id"]]).strip()
            require(value == "-2", "DELETE maintenance lease exists or Redis check failed")

    def capabilities(self, rows):
        results = []
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for row in rows:
            # IDs came from PostgreSQL uuid fields; no user-provided SQL values.
            token = self.sql('SELECT "apiKey" FROM runner WHERE id=\'' + row["id"] + "'::uuid")
            req = urllib.request.Request("http://127.0.0.1:3000/api/jobs/admission/capabilities",
                                         headers={"Authorization": "Bearer " + token})
            with opener.open(req, timeout=10) as response:
                value = json.load(response)
            require(value.get("version") == 1 and value.get("recoveryRenewal") is True,
                    "Runner admission capabilities failed")
            results.append({"name": row["name"], "capabilities": value})
        return results

    def inventory(self, image):
        code = ("const fs=require('fs');let d='/daytona/apps/api/src/migrations';"
                "console.log(JSON.stringify(fs.readdirSync(d,{recursive:true})"
                ".filter(x=>/(^|\\/)[0-9]+-migration\\.ts$/.test(x))))")
        return json.loads(self.command(["docker", "run", "--rm", "--pull=never", "--network", "none",
                                        "--entrypoint", "node", image, "-e", code]))

    def backup(self):
        before = self.state_snapshot()["counts"]
        identity = self.db_identity()
        container = self.inspect(DB)
        values = env_map(container["Config"]["Env"])
        args = ["docker", "exec", DB, "pg_dump", "-Fc", "--lock-wait-timeout=2s",
                "-U", values.get("POSTGRES_USER", "postgres"), "-d", values.get("POSTGRES_DB", "daytona")]
        path = self.state / "fresh.dump"
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            result = subprocess.run(args, stdout=stream, stderr=subprocess.PIPE, timeout=180)
        write(self.state / "backup.private.log", result.stderr)
        require(result.returncode == 0 and path.stat().st_size > 0, "Fresh pg_dump failed")
        require(before == self.state_snapshot()["counts"] and identity == self.db_identity(),
                "Database identity or aggregate counts changed during backup; prepare again")
        with path.open("rb") as stream:
            result = subprocess.run(["docker", "exec", "-i", DB, "pg_restore", "--list"],
                                    stdin=stream, capture_output=True, timeout=30)
        write(self.state / "backup-list.private.log", result.stdout + result.stderr)
        require(result.returncode == 0, "Fresh dump catalog could not be read")
        save(self.state / "expected-counts.json", before)
        return {"path": str(path), "sha256": sha(path.read_bytes()), "bytes": path.stat().st_size,
                "sourceContainerId": identity["containerId"], "counts": before,
                "finishedAt": now(), "consistentSnapshot": True}

    def health(self, seconds=120):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                with opener.open("http://127.0.0.1:3000/api/config", timeout=4) as response:
                    if response.status == 200 and self.inspect(API)["State"].get("Health", {}).get("Status") == "healthy":
                        return {"configHTTP": 200, "dockerHealth": "healthy", "at": now()}
            except Exception:
                pass
            time.sleep(2)
        raise RuntimeError("API config health did not reach HTTP 200")

    def stop(self, container):
        self.command(["docker", "stop", "--time", "30", container], timeout=45,
                     log="stop-api.private.log")

    def migrate(self, plan):
        values = env_map(plan["oldRuntime"]["Config"]["Env"])
        require(not any("\n" in k or "\n" in v or "\r" in v for k, v in values.items()),
                "Environment cannot be encoded safely")
        # This exact runtime pg version and TypeORM CLI were verified under a held
        # DDL lock: PGOPTIONS applies 2s lock / 120s statement timeouts.
        values["PGOPTIONS"] = "-c lock_timeout=2s -c statement_timeout=120s"
        env = self.state / "migration.env"
        write(env, "\n".join(k + "=" + v for k, v in values.items()) + "\n")
        name = "dsec-v190-migrate-" + uuid.uuid4().hex[:12]
        container = None
        self.migration_unconfirmed = True
        save(self.state / "migration-container.json", {"name": name, "cleanupVerified": False})
        try:
            container = self.command(["docker", "run", "-d", "--name", name, "--pull=never",
                "--network", plan["oldRuntime"]["HostConfig"]["NetworkMode"],
                "--cpus", "2", "--memory", "4g", "--env-file", str(env),
                "--entrypoint", "yarn", plan["imageId"], "migration:run:pre-deploy"]).strip()
            require(re.fullmatch(r"[0-9a-f]{64}", container) is not None, "Migration container identity unavailable")
            code = self.command(["docker", "wait", container], timeout=600).strip()
            self.command(["docker", "logs", container], log="migrations.private.log")
            require(code == "0", "Migration process failed; schema is preserved for image rollback")
        finally:
            if container:
                self.command(["docker", "rm", "-f", container], log="migration-cleanup.private.log")
                self.migration_unconfirmed = False
                save(self.state / "migration-container.json", {"name": name, "id": container, "cleanupVerified": True})
            env.unlink(missing_ok=True)

    def up(self, overlay, log):
        write(OVERLAY, overlay)
        self.command(self.compose() + ["up", "-d", "--no-deps", "--pull", "never", "api"],
                     timeout=180, log=log)

    def rollback_guard(self):
        new_states = int(self.sql("""SELECT count(*) FROM sandbox WHERE state::text IN
('pausing','paused','resuming') OR "desiredState"::text IN ('pausing','paused','resuming')"""))
        present = self.sql("""SELECT count(*) FROM information_schema.columns WHERE
table_schema='public' AND table_name='sandbox' AND column_name='domainAllowList'""") == "1"
        used = int(self.sql('SELECT count(*) FROM sandbox WHERE "domainAllowList" IS NOT NULL')) if present else 0
        return {"newStates": new_states, "domainAllowListUsed": used,
                "domainAllowListColumnExists": present}

    def wait_gates(self, plan, started_at):
        deadline = time.monotonic() + 150
        while True:
            state = self.state_snapshot()
            try:
                validate_gates(state, plan["before"]["sandboxes"], plan["before"]["runners"], started_at)
                self.leases(state["runners"])
                return state
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)


def validate_live_compose(old, resolved, image):
    labels = old["Config"].get("Labels", {})
    require(labels.get("com.docker.compose.project") == "daytona"
            and labels.get("com.docker.compose.service") == "api"
            and labels.get("com.docker.compose.project.config_files", "").split(",") == FILES,
            "Unexpected compose project/service/file order")
    require(old["Mounts"] == [] and not resolved["services"]["api"].get("volumes"),
            "Unexpected API mounts")
    expected = env_map(image["Config"].get("Env", []))
    expected.update({k: str(v) if v is not None else "" for k, v in
                     resolved["services"]["api"].get("environment", {}).items()})
    require(expected == env_map(old["Config"]["Env"]), "Resolved compose and actual API environment differ")
    require(expected.get("RUN_MIGRATIONS") == "true", "RUN_MIGRATIONS must remain true")


def prepare(host, image_ref, expected_old_image):
    revision = host.revision()
    candidate = host.image(image_ref)
    validate_candidate(candidate, revision)
    old = host.inspect(API)
    require(old["State"]["Running"] and old["Image"] == expected_old_image,
            "Live API differs from reviewed old image or is stopped")
    resolved = host.config()
    validate_live_compose(old, resolved, host.image(old["Image"]))
    before = host.state_snapshot()
    validate_gates(before)
    validate_migrations(before["migrations"], before["migrations"])
    require(before["superAdminPermissions"] is not None, "SUPER_ADMIN permissions unavailable")
    host.leases(before["runners"])
    host.capabilities(before["runners"])
    inventory = host.inventory(candidate["Id"])
    validate_inventory(inventory, before["migrations"])
    candidate_overlay = json.dumps({"services": {"api": {"image": image_ref}}}) + "\n"
    write(host.state / "candidate-overlay.json", candidate_overlay)
    proposed = host.config(host.state / "candidate-overlay.json")
    expected = copy.deepcopy(resolved)
    expected["services"]["api"]["image"] = image_ref
    require(proposed == expected, "Candidate overlay changes configuration beyond API image")
    file_hashes = host.files()
    original_overlay = Path(OVERLAY).read_text()
    backup = host.backup()
    after = host.state_snapshot()
    validate_gates(after, before["sandboxes"], before["runners"])
    require(after["migrations"] == before["migrations"], "Migration history changed during prepare")
    require(host.files() == file_hashes and runtime(host.inspect(API)) == runtime(old)
            and host.config() == resolved, "Production configuration changed during prepare")
    plan = {"version": 1, "preparedAt": now(), "sourceRevision": revision, "imageRef": image_ref,
            "imageId": candidate["Id"], "oldRuntime": runtime(old), "oldStartedAt": old["State"]["StartedAt"],
            "oldImage": old["Image"], "dbIdentity": host.db_identity(), "files": file_hashes,
            "resolved": resolved, "proposed": proposed, "candidateOverlay": candidate_overlay,
            "originalOverlay": original_overlay, "before": before, "freshBackup": backup,
            "imageMigrations": inventory, "superAdminPermissionsBefore": before["superAdminPermissions"]}
    save(host.state / "plan.json", plan)
    digest = sha((host.state / "plan.json").read_bytes())
    write(host.state / "plan.sha256", digest + "\n")
    return {"status": "prepared_not_applied", "planSha256": digest, "sourceRevision": revision,
            "imageId": candidate["Id"], "backup": backup, "protectedSandboxCount": 80}


def preflight(host, plan, digest):
    require(sha(private(host.state / "plan.json").read_bytes()) == digest, "Reviewed plan digest differs")
    require(host.revision() == plan["sourceRevision"], "Committed source changed")
    candidate = host.image(plan["imageRef"])
    validate_candidate(candidate, plan["sourceRevision"])
    require(candidate["Id"] == plan["imageId"], "Candidate image tag changed")
    require(host.db_identity() == plan["dbIdentity"], "Database container/config identity changed")
    require(runtime(host.inspect(API)) == plan["oldRuntime"], "Live API identity/config drift")
    require(host.inspect(API)["State"]["Running"], "Live API is not running")
    require(host.files() == plan["files"] and host.config() == plan["resolved"], "Compose/env configuration drift")
    proof = json.loads(private(host.state / "fresh-backup-restore-proof.json").read_text())
    validate_restore(plan, proof, host.state / "fresh.dump")
    state = host.state_snapshot()
    require(state["migrations"] == plan["before"]["migrations"], "Migration history drift")
    require(state["superAdminPermissions"] == plan["superAdminPermissionsBefore"], "SUPER_ADMIN permissions drift")
    validate_gates(state, plan["before"]["sandboxes"], plan["before"]["runners"])
    host.leases(state["runners"])
    host.capabilities(state["runners"])
    return state


def verify_new_runtime(host, plan, new):
    require(new["State"]["Running"] and new["Image"] == plan["imageId"]
            and new["Config"].get("Labels", {}).get(REVISION_LABEL) == plan["sourceRevision"],
            "New API container/image/source identity mismatch")
    validate_live_compose(new, host.config(), host.image(plan["imageId"]))
    require(host.config() == plan["proposed"], "New compose configuration differs from reviewed plan")
    # Docker image defaults (CMD/entrypoint/env) may legitimately change; host runtime may not.
    old_host, new_host = copy.deepcopy(plan["oldRuntime"]["HostConfig"]), copy.deepcopy(new["HostConfig"])
    require(old_host == new_host and new["Mounts"] == plan["oldRuntime"]["Mounts"],
            "API host configuration/mounts changed")


def rollback(host, plan):
    require(not getattr(host, "migration_unconfirmed", False),
            "Rollback blocked: migration container cleanup is unconfirmed; inspect private evidence")
    require(host.db_identity() == plan["dbIdentity"], "Rollback refused: database identity drift")
    live = host.inspect(API)
    require(live["Image"] in (plan["oldImage"], plan["imageId"]), "Rollback refused: independent API deployment")
    hashes = host.files()
    require(all(hashes.get(k) == v for k, v in plan["files"].items() if k != OVERLAY),
            "Rollback refused: compose/env drift outside owned overlay")
    require(Path(OVERLAY).read_text() in (plan["originalOverlay"], plan["candidateOverlay"]),
            "Rollback refused: independent overlay edit")
    state = host.state_snapshot()
    validate_migrations(plan["before"]["migrations"], state["migrations"])
    guard = host.rollback_guard()
    save(host.state / "rollback-guard.json", guard)
    validate_rollback(guard)
    # Retag the original image ref used in the reviewed overlay only after verifying
    # it still resolves to the original immutable ID. Never move an unrelated tag.
    old_ref = plan["resolved"]["services"]["api"]["image"]
    require(host.image(old_ref)["Id"] == plan["oldImage"], "Rollback refused: original image tag drift")
    host.up(plan["originalOverlay"], "rollback.private.log")
    health = host.health()
    restored = host.inspect(API)
    require(restored["Image"] == plan["oldImage"], "Old API image rollback verification failed")
    validate_live_compose(restored, host.config(), host.image(plan["oldImage"]))
    state = host.wait_gates(plan, restored["State"]["StartedAt"])
    validate_migrations(plan["before"]["migrations"], state["migrations"])
    return {"status": "rolled_back", "schemaPreserved": True, "migrationCount": len(state["migrations"]),
            "imageId": restored["Image"], "containerId": restored["Id"], "health": health,
            "guard": guard}


def apply(host, plan, digest, mode):
    require(mode == "confirm-production-change", "Explicit production apply mode required")
    preflight(host, plan, digest)
    # Consume plan once: replay requires a new backup/review. This is not an approval prompt.
    fd = os.open(host.state / "apply-started.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"at": now(), "planSha256": digest}, stream)
    try:
        # Briefly stop only API schedulers before DDL. Existing Runner sandboxes keep running.
        host.stop(plan["oldRuntime"]["Id"])
        host.migrate(plan)
        state = host.state_snapshot()
        validate_migrations(plan["before"]["migrations"], state["migrations"], complete=True)
        require(host.files() == plan["files"], "Configuration drift during migration")
        require(host.image(plan["imageRef"])["Id"] == plan["imageId"], "Candidate tag drift during migration")
        host.up(plan["candidateOverlay"], "compose-up.private.log")
        health = host.health()
        new = host.inspect(API)
        verify_new_runtime(host, plan, new)
        state = host.wait_gates(plan, new["State"]["StartedAt"])
        validate_migrations(plan["before"]["migrations"], state["migrations"], complete=True)
        capabilities = host.capabilities(state["runners"])
        for row in state["runners"]:
            row["lastHealthcheckAt"] = row["lastChecked"]
        receipt = {"status": "success", "sourceRevision": plan["sourceRevision"],
                   "imageId": plan["imageId"], "imageRef": plan["imageRef"], "containerId": new["Id"],
                   "startedAt": new["State"]["StartedAt"], "health": health, "runners": state["runners"],
                   "migrationCount": len(state["migrations"]), "migrations": EXPECTED,
                   "protectedSandboxes": state["sandboxes"], "capabilities": capabilities,
                   "backupSha256": plan["freshBackup"]["sha256"], "finishedAt": now(),
                   "superAdminPermissionsBefore": plan["superAdminPermissionsBefore"],
                   "superAdminPermissionsAfter": state["superAdminPermissions"]}
        save(host.state / "report.json", receipt)
        return receipt
    except Exception as error:
        # Do not expose query results, env or command stdout in public error messages.
        result = {"status": "failed", "errorType": type(error).__name__, "error": str(error),
                  "at": now(), "schemaDowngraded": False}
        try:
            result["rollback"] = rollback(host, plan)
            result["status"] = "rolled_back"
        except Exception as rollback_error:
            result["status"] = "rollback_blocked"
            result["rollbackError"] = str(rollback_error)
        save(host.state / "report.json", result)
        raise RuntimeError("Upgrade failed; inspect private report.json for rollback outcome") from None


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--image", required=True)
    prepare_parser.add_argument("--expected-old-image", required=True)
    prepare_parser.add_argument("--state-dir", required=True)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--state-dir", required=True)
    apply_parser.add_argument("--expected-plan-sha256", required=True)
    apply_parser.add_argument("--mode", required=True, choices=["confirm-production-change"])
    args = parser.parse_args()
    state = Path(args.state_dir).resolve()
    if args.action == "prepare":
        state.mkdir(mode=0o700, parents=True, exist_ok=False)
    require(state.is_dir() and state.stat().st_mode & 0o077 == 0, "State directory must be private")
    host = Host(state)
    with open("/opt/codex-dsec-20260930/upgrade-v0190-api.lock", "a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "prepare":
            result = prepare(host, args.image, args.expected_old_image)
        else:
            plan = json.loads(private(state / "plan.json").read_text())
            result = apply(host, plan, args.expected_plan_sha256, args.mode)
    print(json.dumps({k: result[k] for k in ("status", "sourceRevision", "imageId", "containerId",
                     "migrationCount", "planSha256", "protectedSandboxCount") if k in result}))


if __name__ == "__main__":
    main()
