"""Synthetic-only checks for the source-overlay exclusion boundary."""

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


SPEC = importlib.util.spec_from_file_location(
    "source_archive", Path(__file__).parents[1] / "scripts/package-conversation-source.py")
ARCHIVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ARCHIVE)


class SourceArchive(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        self.output = self.base / "conversational-release-source-test.zip"
        for name in ARCHIVE.REQUIRED:
            self.write(name, "public source\n")
        self.images = {name: "sha256:" + "1" * 64 for name in ("api", "mcp", "pwa", "homebox")}

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def build(self, **overrides):
        arguments = dict(root=self.root, output=self.output, release="synthetic-test",
                         base_revision="a" * 40, images=self.images, approved=True)
        arguments.update(overrides)
        return ARCHIVE.build(**arguments)

    def test_only_public_source_and_neutral_template_are_included(self):
        private = "NEVER_INCLUDE_PRIVATE_FIXTURE"
        for name in (
            "deploy/.env", "deploy/.env.before", "apps/pwa/.env.example",
            "deploy/secrets/key", "deploy/certs/ca.pem", "deploy/backups/backup.py",
            "deploy/private/report.md", "deploy/runtime/credentials.json",
            "apps/pwa/node_modules/dependency/index.js", "apps/pwa/dist/index.html",
            "apps/pwa/test-results/screenshot.png", "apps/assistant-api/.pytest_cache/readme.md",
            "apps/assistant-api/assistant/__pycache__/state.pyc",
            "apps/assistant-api/uploads/photo.jpg", "deploy/unknown.key",
            "apps/pwa/public/unknown-photo.png",
            "apps/pwa/.npmrc", "apps/pwa/.auth/state.json", "deploy/credentials.json",
            "deploy/storageState.json", "deploy/Secrets/settings.json", "deploy/.ENV",
            "apps/pwa/test-results/oldfirst-failure-private.zip",
            "apps/pwa/auth/state.json", "apps/pwa/checkpoints/session.json",
            "apps/pwa/.checkpoints/session.json", "apps/pwa/trace/events.json",
            "apps/pwa/traces/events.json", "apps/pwa/checkpoint.json",
            "apps/pwa/trace.json", "apps/pwa/trace.zip",
        ):
            self.write(name, private)
        source = self.write("deploy/scripts/helper.sh", "#!/bin/sh\nexit 0\n")
        source.chmod(0o755)
        self.build()
        with zipfile.ZipFile(self.output) as archive:
            names = set(archive.namelist())
            self.assertEqual(names, ARCHIVE.REQUIRED | {
                "deploy/scripts/helper.sh", "SOURCE-MANIFEST.json", "SOURCE-README.md"})
            self.assertTrue(all(private.encode() not in archive.read(name) for name in names))
            manifest = json.loads(archive.read("SOURCE-MANIFEST.json"))
            for entry in manifest["files"]:
                data = archive.read(entry["path"])
                self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(entry["bytes"], len(data))
            self.assertEqual((archive.getinfo("deploy/scripts/helper.sh").external_attr >> 16) & 0o777, 0o755)
            self.assertIsNone(archive.testzip())

    def test_only_named_public_binary_icons_are_included(self):
        icon = self.root / "apps/pwa/public/icon-192.png"
        icon.parent.mkdir(parents=True)
        icon.write_bytes(b"\x89PNG\r\n\x1a\n\xff")
        self.build()
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(archive.read("apps/pwa/public/icon-192.png"), icon.read_bytes())

    def test_no_final_approval_creates_no_artifact(self):
        with self.assertRaisesRegex(ValueError, "approval"):
            self.build(approved=False)
        self.assertFalse(self.output.exists())

    def test_existing_or_historical_artifacts_are_never_overwritten(self):
        self.output.write_bytes(b"preserved")
        with self.assertRaises(FileExistsError):
            self.build()
        self.assertEqual(self.output.read_bytes(), b"preserved")
        with self.assertRaisesRegex(ValueError, "new conversational"):
            self.build(output=self.base / "source.zip")

    def test_source_symlinks_are_rejected(self):
        outside = self.base / "private.py"
        outside.write_text("private")
        target = self.root / "apps/pwa/link.py"
        target.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_output_inside_source_roots_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            self.build(output=self.root / "deploy" / self.output.name)

    def test_missing_required_source_is_rejected(self):
        (self.root / "apps/pwa/src/App.vue").unlink()
        with self.assertRaisesRegex(ValueError, "Required"):
            self.build()

    def test_source_directory_symlink_is_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.root / "apps/pwa/linked").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.build()


if __name__ == "__main__":
    unittest.main()
