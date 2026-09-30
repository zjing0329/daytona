#!/usr/bin/env python3
"""Offline tests: no Docker, network, database or production writes."""
import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("deploy_api", Path(__file__).with_name("deploy_api.py"))
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


def migrations(n=0):
    return [{"id": i + 1, "timestamp": 1000000000000 + i, "name": "Old" + str(i)}
            for i in range(109)] + [
        {"id": 110 + i, "timestamp": stamp, "name": "Upgrade" + str(i)}
        for i, stamp in enumerate(d.EXPECTED[:n])]


def state(n=0):
    return {"activeJobs": 0, "otherActiveRunners": 0, "migrations": migrations(n),
            "counts": {"migrations": 109 + n, "sandbox_count": 1507, "job_count": 42},
            "superAdminPermissions": ["old-permission"],
            "runners": [{"id": name, "name": name, "apiUrl": url, "unschedulable": False,
                         "draining": False, "state": "ready", "lastChecked": d.now()}
                        for name, url in sorted(d.TARGETS.items())],
            "sandboxes": [{"id": str(i), "state": "started", "desiredState": "started",
                           "runnerId": "a2"} for i in range(80)]}


class FakeHost:
    def __init__(self, directory):
        self.state = Path(directory)
        self.calls = []
        self.partial = 0
        self.fail = None
        self.rev = "a" * 40
        self.cid = "old-container"
        self.image_id = "old-image"
        self.running = True
        self.stamp = d.now()
        self.env = ["RUN_MIGRATIONS=true"]
        self.labels = {"com.docker.compose.project": "daytona",
                       "com.docker.compose.service": "api",
                       "com.docker.compose.project.config_files": ",".join(d.FILES)}
        self.hashes = {path: "original" for path in d.FILES}
        self.config_value = {"services": {"api": {"image": "old-ref",
                                                   "environment": {"RUN_MIGRATIONS": "true"}}}}
        self.original = '{"services":{"api":{"image":"old-ref"}}}\n'
        self.candidate = '{"services":{"api":{"image":"new-ref"}}}\n'
        self.overlay = self.state / "live-overlay"
        self.overlay.write_text(self.original)
        self.guard = {"newStates": 0, "domainAllowListUsed": 0}
        self.snapshot = state()
        self.db = {"containerId": "db"}
        self.runtime = copy.deepcopy(d.runtime(self.inspect(d.API)))
        self.plan = {"sourceRevision": self.rev, "imageRef": "new-ref", "imageId": "new-image",
                     "oldImage": "old-image", "oldRuntime": self.runtime, "files": copy.deepcopy(self.hashes),
                     "dbIdentity": self.db, "resolved": copy.deepcopy(self.config_value),
                     "proposed": {"services": {"api": {"image": "new-ref",
                                                       "environment": {"RUN_MIGRATIONS": "true"}}}},
                     "before": copy.deepcopy(self.snapshot), "originalOverlay": self.original,
                     "candidateOverlay": self.candidate, "preparedAt": d.now(),
                     "superAdminPermissionsBefore": ["old-permission"]}
        d.write(self.state / "fresh.dump", b"consistent-backup")
        self.plan["freshBackup"] = {"sourceContainerId": "db", "sha256": d.sha(b"consistent-backup"),
                                   "counts": self.snapshot["counts"], "finishedAt": d.now()}
        self.proof = {"status": "success", "productionModified": False, "cleanupVerified": True,
                      "sourceContainerId": "db", "backupSha256": d.sha(b"consistent-backup"),
                      "restored": self.snapshot["counts"], "finishedAt": d.now()}
        self.persist()

    def persist(self):
        d.save(self.state / "plan.json", self.plan)
        d.save(self.state / "fresh-backup-restore-proof.json", self.proof)
        self.digest = d.sha((self.state / "plan.json").read_bytes())

    def revision(self): return self.rev
    def image(self, ref):
        image_id = "new-image" if ref in ("new-ref", "new-image") else "old-image"
        return {"Id": image_id, "Config": {"Env": [], "Labels": {d.REVISION_LABEL: self.rev}}}
    def inspect(self, name):
        return {"Id": self.cid, "Image": self.image_id, "Config": {"Env": self.env,
                "Labels": {**self.labels, d.REVISION_LABEL: self.rev}},
                "HostConfig": {"NetworkMode": "production-network"}, "Mounts": [],
                "State": {"Running": self.running, "StartedAt": self.stamp}}
    def db_identity(self): return self.db
    def files(self): return self.hashes
    def config(self): return self.config_value
    def state_snapshot(self):
        value = copy.deepcopy(self.snapshot)
        value["migrations"] = migrations(self.partial)
        for r in value["runners"]: r["lastChecked"] = d.now()
        return value
    def leases(self, rows): self.calls.append("leases")
    def capabilities(self, rows): self.calls.append("capabilities"); return []
    def stop(self, cid):
        self.calls.append("stop")
        self.running = False
    def migrate(self, plan):
        self.calls.append("migrate")
        if self.fail == "migration":
            self.partial = 2
            raise RuntimeError("bounded migration failed")
        self.partial = 4
    def up(self, overlay, log):
        self.calls.append("up-old" if overlay == self.original else "up-new")
        self.overlay.write_text(overlay)
        self.image_id = "old-image" if overlay == self.original else "new-image"
        self.cid = "restored-container" if overlay == self.original else "new-container"
        self.running = True
        self.stamp = d.now()
        self.config_value = copy.deepcopy(self.plan["resolved"] if overlay == self.original else self.plan["proposed"])
    def health(self):
        if self.fail == "new-health" and self.image_id == "new-image":
            raise RuntimeError("new image unhealthy")
        return {"configHTTP": 200, "at": d.now()}
    def wait_gates(self, plan, after):
        result = self.state_snapshot()
        d.validate_gates(result, plan["before"]["sandboxes"], plan["before"]["runners"], after)
        return result
    def rollback_guard(self): return self.guard


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.h = FakeHost(self.directory.name)
        self.overlay_patch = patch.object(d, "OVERLAY", str(self.h.overlay))
        self.overlay_patch.start()
        self.addCleanup(self.overlay_patch.stop)

    def apply(self):
        return d.apply(self.h, self.h.plan, self.h.digest, "confirm-production-change")

    def test_explicit_mode_required_before_any_operations(self):
        with self.assertRaisesRegex(RuntimeError, "Explicit"):
            d.apply(self.h, self.h.plan, self.h.digest, "preview")
        self.assertEqual(self.h.calls, [])

    def test_success_does_exactly_one_api_stop_and_four_migrations(self):
        receipt = self.apply()
        self.assertEqual(receipt["status"], "success")
        self.assertEqual(receipt["migrationCount"], 113)
        self.assertEqual(receipt["migrations"], d.EXPECTED)
        self.assertEqual([c for c in self.h.calls if c in ("stop", "migrate", "up-new", "up-old")],
                         ["stop", "migrate", "up-new"])
        self.assertEqual(len(receipt["protectedSandboxes"]), 80)

    def test_partial_migration_failure_restores_old_image_without_schema_down(self):
        self.h.fail = "migration"
        with self.assertRaisesRegex(RuntimeError, "Upgrade failed"): self.apply()
        self.assertEqual(self.h.partial, 2)
        self.assertEqual(self.h.image_id, "old-image")
        report = json.loads((self.h.state / "report.json").read_text())
        self.assertEqual(report["status"], "rolled_back")
        self.assertEqual(report["rollback"]["migrationCount"], 111)
        self.assertTrue(report["rollback"]["schemaPreserved"])
        self.assertNotIn("up-new", self.h.calls)

    def test_new_api_health_failure_preserves_full_schema_and_rolls_back_image(self):
        self.h.fail = "new-health"
        with self.assertRaises(RuntimeError): self.apply()
        self.assertEqual(self.h.partial, 4)
        self.assertEqual(self.h.image_id, "old-image")
        self.assertEqual(self.h.calls.count("up-old"), 1)

    def test_unsafe_new_data_blocks_image_rollback(self):
        self.h.fail = "new-health"
        self.h.guard["newStates"] = 1
        with self.assertRaises(RuntimeError): self.apply()
        report = json.loads((self.h.state / "report.json").read_text())
        self.assertEqual(report["status"], "rollback_blocked")
        self.assertNotIn("up-old", self.h.calls)

    def test_domain_allow_list_blocks_rollback(self):
        with self.assertRaisesRegex(RuntimeError, "domainAllowList"):
            d.validate_rollback({"newStates": 0, "domainAllowListUsed": 1})

    def test_plan_replay_is_rejected_before_second_stop(self):
        self.apply()
        # Restore preflight fixture while retaining consumed plan marker.
        with patch.object(d, "preflight", return_value=state()):
            with self.assertRaises(FileExistsError): self.apply()
        self.assertEqual(self.h.calls.count("stop"), 1)

    def test_wrong_revision_or_changed_image_blocks_before_stop(self):
        for replacement in [
            {"Id": "new-image", "Config": {"Labels": {d.REVISION_LABEL: "b" * 40}}},
            {"Id": "different", "Config": {"Labels": {d.REVISION_LABEL: self.h.rev}}},
        ]:
            with self.subTest(replacement=replacement), patch.object(self.h, "image", return_value=replacement):
                with self.assertRaises(RuntimeError): self.apply()
            self.assertNotIn("stop", self.h.calls)

    def test_runtime_and_compose_drift_block_before_stop(self):
        self.h.env.append("UNREVIEWED=true")
        with self.assertRaisesRegex(RuntimeError, "identity/config drift"): self.apply()
        self.h.env.pop()
        self.h.hashes[d.FILES[0]] = "changed"
        with self.assertRaisesRegex(RuntimeError, "configuration drift"): self.apply()
        self.assertNotIn("stop", self.h.calls)

    def test_stale_missing_wrong_source_or_wrong_digest_restore_proof_blocks(self):
        cases = [
            {"finishedAt": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=901)).isoformat()},
            {"sourceContainerId": "other-db"}, {"backupSha256": "wrong"},
            {"status": "failed"}, {"cleanupVerified": False}, {"productionModified": True},
        ]
        original = copy.deepcopy(self.h.proof)
        for changes in cases:
            self.h.proof = {**original, **changes}; self.h.persist()
            with self.subTest(changes=changes), self.assertRaises(RuntimeError): self.apply()
        (self.h.state / "fresh-backup-restore-proof.json").unlink()
        with self.assertRaises(RuntimeError): self.apply()
        self.assertNotIn("stop", self.h.calls)

    def test_changed_dump_blocks_even_with_valid_proof(self):
        d.write(self.h.state / "fresh.dump", b"changed")
        with self.assertRaisesRegex(RuntimeError, "digest"): self.apply()
        self.assertNotIn("stop", self.h.calls)

    def test_nonprefix_partial_migrations_rejected(self):
        altered = migrations(2)
        altered[-1]["timestamp"] = d.EXPECTED[3]
        with self.assertRaisesRegex(RuntimeError, "out-of-order"):
            d.validate_migrations(migrations(), altered)

    def test_baseline_history_mutation_rejected(self):
        altered = migrations(4)
        altered[12]["name"] = "modified"
        with self.assertRaisesRegex(RuntimeError, "history"):
            d.validate_migrations(migrations(), altered, complete=True)

    def test_protected_ids_active_jobs_and_runner_gates(self):
        for mutate in [
            lambda s: s.update(activeJobs=1),
            lambda s: s.update(otherActiveRunners=1),
            lambda s: s["sandboxes"][0].update(id="different"),
            lambda s: s["sandboxes"][0].update(desiredState="stopped"),
            lambda s: s["runners"][0].update(draining=True),
            lambda s: s["runners"][0].update(unschedulable=True),
            lambda s: s["runners"][0].update(lastChecked="2020-01-01T00:00:00Z"),
        ]:
            altered = state(); mutate(altered)
            with self.assertRaises(RuntimeError):
                d.validate_gates(altered, self.h.plan["before"]["sandboxes"])

    def test_timestamp_fraction_and_after_start(self):
        self.assertEqual(d.timestamp("2026-01-01T00:00:00.1Z").microsecond, 100000)
        self.assertEqual(d.timestamp("2026-01-01T00:00:00.123456789Z").microsecond, 123456)
        with self.assertRaisesRegex(RuntimeError, "predates"):
            d.validate_gates(state(), after=(dt.datetime.now(dt.timezone.utc) +
                                           dt.timedelta(seconds=5)).isoformat())

    def test_failed_pg_dump_never_returns_usable_backup(self):
        h = d.Host(self.directory.name)
        (h.state / "fresh.dump").unlink()
        with patch.object(h, "state_snapshot", return_value=state()), \
             patch.object(h, "db_identity", return_value={"containerId": "db"}), \
             patch.object(h, "inspect", return_value={"Config": {"Env": []}}), \
             patch.object(d.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stderr=b"failure")):
            with self.assertRaisesRegex(RuntimeError, "pg_dump"): h.backup()
        self.assertFalse((h.state / "expected-counts.json").exists())

    def test_migration_uses_verified_pgoptions_and_cleans_owned_container(self):
        h = d.Host(self.directory.name)
        commands = []
        def command(args, **kwargs):
            commands.append(args)
            if args[:3] == ["docker", "run", "-d"]:
                values = (h.state / "migration.env").read_text()
                self.assertIn("PGOPTIONS=-c lock_timeout=2s -c statement_timeout=120s", values)
                return "f" * 64
            if args[:2] == ["docker", "wait"]: return "1"
            return ""
        with patch.object(h, "command", side_effect=command):
            with self.assertRaisesRegex(RuntimeError, "Migration process failed"): h.migrate(self.h.plan)
        self.assertEqual(commands[-1], ["docker", "rm", "-f", "f" * 64])
        self.assertFalse((h.state / "migration.env").exists())
        self.assertNotIn("down", " ".join(sum(commands, [])))

    def test_unconfirmed_migration_container_blocks_old_api_restart(self):
        self.h.migration_unconfirmed = True
        with self.assertRaisesRegex(RuntimeError, "cleanup is unconfirmed"):
            d.rollback(self.h, self.h.plan)
        self.assertNotIn("up-old", self.h.calls)

    def test_inventory_covers_history_and_only_four_predeploy_pending(self):
        paths = [str(row["timestamp"]) + "-migration.ts" for row in migrations()]
        paths += ["pre-deploy/" + str(stamp) + "-migration.ts" for stamp in d.EXPECTED]
        d.validate_inventory(paths, migrations())
        altered = paths.copy()
        altered[-1] = altered[-1].replace("pre-deploy", "post-deploy")
        with self.assertRaisesRegex(RuntimeError, "pre-deploy"):
            d.validate_inventory(altered, migrations())
        with self.assertRaisesRegex(RuntimeError, "full migration"):
            d.validate_inventory(paths[1:], migrations())
        with self.assertRaisesRegex(RuntimeError, "full migration"):
            d.validate_inventory(paths + ["9999999999999-migration.ts"], migrations())

    def test_prepared_artifacts_are_private(self):
        for path in self.h.state.iterdir():
            if path.name != "live-overlay":
                self.assertEqual(path.stat().st_mode & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
