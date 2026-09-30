import importlib.util
import hashlib
import json
from unittest import mock
import build_receipt as receipt
import pathlib
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("package_image", pathlib.Path(__file__).with_name("package-complete-image.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        lines = []
        for name in m.ASSETS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x7fELF" + name.encode())
            lines.append(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + name)
        self.manifest = self.root / "dist/runner-assets.sha256"
        self.manifest.write_text("\n".join(lines) + "\n")

    def receipt_fixture(self):
        for name in receipt.BUILD_ROOTS:
            (self.root / name).mkdir(parents=True, exist_ok=True)
        for name in ("go.work", "go.work.sum", *receipt.RECIPES):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
        inputs = receipt.input_hashes(self.root)
        value = dict(complete=True, source_revision="a" * 40, inputs=inputs,
            input_fingerprint=receipt.fingerprint(inputs), outputs=m.verify_assets(self.root),
            workspace_checksum_sha256=receipt.sha(self.root / "go.work.sum"))
        (self.root / "dist/build-receipt.json").write_text(json.dumps(value))
        return value

    def test_runtime_base_layers_must_match(self):
        base = {"RootFS": {"Layers": ["original"]}, "Config": {"Cmd": ["runner"]}}
        candidate = {"RootFS": {"Layers": ["original", "new-binary"]}, "Config": {"Cmd": ["runner"]}}
        self.assertTrue(m.runtime_matches_base(base, candidate))
        candidate["RootFS"]["Layers"][0] = "other-base"
        self.assertFalse(m.runtime_matches_base(base, candidate))

    def test_runtime_config_must_match(self):
        base = {"RootFS": {"Layers": ["original"]}, "Config": {"Cmd": ["runner"]}}
        candidate = {"RootFS": {"Layers": ["original", "new-binary"]}, "Config": {"Cmd": ["other"]}}
        self.assertFalse(m.runtime_matches_base(base, candidate))

    def test_complete_assets(self):
        self.assertEqual(set(m.verify_assets(self.root)), set(m.ASSETS))

    def test_incomplete_daemon_manifest_rejected(self):
        self.manifest.write_text("\n".join(self.manifest.read_text().splitlines()[1:]))
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            m.verify_assets(self.root)

    def test_tampered_embedded_resource_rejected(self):
        (self.root / m.ASSETS[1]).write_bytes(b"wrong")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            m.verify_assets(self.root)

    def test_duplicate_entry_rejected(self):
        self.manifest.write_text(self.manifest.read_text() + self.manifest.read_text().splitlines()[0])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            m.verify_assets(self.root)

    def test_non_elf_with_matching_hash_rejected(self):
        path = self.root / m.ASSETS[1]
        path.write_bytes(b"not an executable")
        lines = self.manifest.read_text().splitlines()
        lines[1] = hashlib.sha256(path.read_bytes()).hexdigest() + "  " + m.ASSETS[1]
        self.manifest.write_text("\n".join(lines))
        with self.assertRaisesRegex(ValueError, "ELF"):
            m.verify_assets(self.root)

    def test_wrong_revision_rejected_before_docker(self):
        with self.assertRaisesRegex(ValueError, "revision"):
            m.verify_source(self.root, self.root, "not-a-commit")

    def test_generated_workspace_sum_recorded_separately(self):
        value = self.receipt_fixture()
        (self.root / "go.work.sum").write_text("generated checksum cache")
        self.assertEqual(value["inputs"], receipt.input_hashes(self.root))
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            receipt.validate(self.root, "a" * 40, m.verify_assets(self.root))
        value["workspace_checksum_sha256"] = receipt.sha(self.root / "go.work.sum")
        (self.root / "dist/build-receipt.json").write_text(json.dumps(value))
        receipt.validate(self.root, "a" * 40, m.verify_assets(self.root))

    def test_workspace_without_optional_sum(self):
        self.receipt_fixture()
        (self.root / "go.work.sum").unlink()
        self.assertNotIn("go.work.sum", receipt.input_hashes(self.root))

    def test_matching_receipt(self):
        value = self.receipt_fixture()
        self.assertEqual(receipt.validate(self.root, "a" * 40, m.verify_assets(self.root)), value)

    def test_old_receipt_new_source_rejected(self):
        self.receipt_fixture()
        (self.root / receipt.RECIPES[0]).write_text("new recipe")
        with self.assertRaisesRegex(ValueError, "inputs mismatch"):
            receipt.validate(self.root, "a" * 40, m.verify_assets(self.root))

    def test_old_binary_with_self_consistent_manifest_rejected(self):
        self.receipt_fixture()
        path = self.root / m.ASSETS[0]
        path.write_bytes(b"\x7fELFold-runner")
        lines = self.manifest.read_text().splitlines()
        lines[0] = hashlib.sha256(path.read_bytes()).hexdigest() + "  " + m.ASSETS[0]
        self.manifest.write_text("\n".join(lines))
        with self.assertRaisesRegex(ValueError, "outputs mismatch"):
            receipt.validate(self.root, "a" * 40, m.verify_assets(self.root))

    def test_extra_go_source_rejected(self):
        self.receipt_fixture()
        (self.root / "apps/runner/untracked.go").write_text("package main")
        with self.assertRaisesRegex(ValueError, "inputs mismatch"):
            receipt.validate(self.root, "a" * 40, m.verify_assets(self.root))

    def test_begin_removes_old_outputs_and_rejects_alternate_workspace(self):
        self.receipt_fixture()
        env = json.dumps({"GOWORK": str(self.root / "other.work")})
        with mock.patch.object(receipt.subprocess, "check_output", return_value=env):
            with self.assertRaisesRegex(ValueError, "go.work"):
                receipt.begin(self.root, "a" * 40, "v0.187.0")
        env = json.dumps({"GOWORK": str(self.root / "go.work")})
        with mock.patch.object(receipt.subprocess, "check_output", side_effect=[env, "go version go1.25.5 linux/amd64"]):
            receipt.begin(self.root, "a" * 40, "v0.187.0")
        self.assertTrue((self.root / "dist/build-start.json").is_file())
        self.assertFalse((self.root / "dist/build-receipt.json").exists())
        for name in m.ASSETS:
            self.assertFalse((self.root / name).exists())


if __name__ == "__main__":
    unittest.main()
