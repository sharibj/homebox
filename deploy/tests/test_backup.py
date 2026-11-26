"""Docker-only stdlib checks for operator-owned backup output."""

import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import unittest


DOCKER = r'''#!/usr/local/bin/python
import io,json,os,sys,tarfile
from pathlib import Path
args=sys.argv[1:]
with Path(os.environ["CALLS"]).open("a") as log:
    log.write(json.dumps(args)+"\n")
if args[0]=="inspect":
    print("/synthetic/deploy/docker-compose.yml"+os.environ.get("OVERLAYS",""))
elif "ps" in args:
    print("synthetic-api" if "-q" in args else
          "proxy\nassistant-api\nhomebox-mcp\nhomebox\npwa\nollama")
elif "run" in args:
    script=args[-1]
    if "tar -czf -" in script:
        print("Synthetic reader startup message",file=sys.stderr)
        mode=os.environ.get("FAILURE","")
        if mode=="stream":
            sys.stdout.buffer.write(b"incomplete-private-stream")
            sys.exit(73)
        if mode=="validation":
            sys.stdout.buffer.write(b"invalid-private-archive")
        else:
            options={part.split("=",1)[0]:part.split("=",1)[1]
                     for part in args if part.startswith("BACKUP_")}
            data=("EXTERNAL_AI="+options["BACKUP_EXTERNAL_AI"]+"\n"+
                  "LAN_HTTPS="+options["BACKUP_LAN_HTTPS"]+"\n").encode()
            with tarfile.open(fileobj=sys.stdout.buffer,mode="w|gz") as archive:
                item=tarfile.TarInfo("deployment-options.env")
                item.size=len(data)
                archive.addfile(item,io.BytesIO(data))
    elif "tar -tzf" in script:
        mount=args[args.index("--volume")+1]
        assert mount.endswith(":/backups:ro")
        directory=Path(mount[:-len(":/backups:ro")])
        name=next(part.split("=",1)[1] for part in args if part.startswith("ARCHIVE="))
        try:
            with tarfile.open(directory/("."+name+".partial"),"r:gz") as archive:
                archive.getmembers()
        except tarfile.TarError:
            sys.exit(74)
    else:
        sys.exit("Unexpected backup reader command")
elif "stop" not in args and "up" not in args:
    sys.exit("Unexpected Docker command")
'''


class BackupOutput(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.deploy = self.root / "deploy"
        self.deploy.mkdir()
        (self.deploy / ".env").write_text("STACK_VERSION=synthetic\n")
        self.output = self.deploy / "backups"
        self.binary = self.root / "bin"
        self.binary.mkdir()
        (self.binary / "docker").write_text(DOCKER)
        (self.binary / "docker").chmod(0o755)
        self.calls = self.root / "calls.jsonl"
        self.env = {
            **os.environ, "PATH": str(self.binary) + ":" + os.environ["PATH"],
            "HOMEBOX_DEPLOY_DIR": str(self.deploy), "CALLS": str(self.calls),
        }

    def run_backup(self, **environment):
        return subprocess.run(
            ["sh", "/source/deploy/scripts/backup.sh", "fixture.tar.gz"],
            env={**self.env, **environment}, capture_output=True, text=True,
            check=False, timeout=15,
        )

    def commands(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def assert_resumed(self):
        calls = self.commands()
        stopped = next(call for call in calls if "stop" in call)
        resumed = next(call for call in calls if "up" in call)
        expected = ["proxy", "assistant-api", "homebox-mcp", "homebox", "pwa"]
        self.assertEqual(stopped[-5:], expected)
        self.assertEqual(resumed[-5:], expected)
        self.assertNotIn("ollama", stopped)
        self.assertNotIn("ollama", resumed)
        self.assertIn("--no-recreate", resumed)
        self.assertIn("--no-build", resumed)

    def test_operator_creates_private_archive_and_reader_never_chowns_it(self):
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        archive = self.output / "fixture.tar.gz"
        self.assertTrue(archive.is_file())
        self.assertFalse((self.output / ".fixture.tar.gz.partial").exists())
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        self.assertEqual((archive.stat().st_uid, archive.stat().st_gid),
                         (os.getuid(), os.getgid()))
        self.assertIn("Created ", result.stdout)
        for call in self.commands():
            self.assertFalse(any(part.startswith(("BACKUP_UID=", "BACKUP_GID=")) for part in call))
        self.assert_resumed()

    def test_stream_failure_retains_partial_and_resumes_without_success(self):
        result = self.run_backup(FAILURE="stream")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Backup stream failed.", result.stderr)
        self.assertNotIn("Created ", result.stdout)
        self.assertEqual((self.output / ".fixture.tar.gz.partial").read_bytes(),
                         b"incomplete-private-stream")
        self.assertEqual(stat.S_IMODE((self.output / ".fixture.tar.gz.partial").stat().st_mode),
                         0o600)
        self.assertFalse((self.output / "fixture.tar.gz").exists())
        self.assert_resumed()

    def test_validation_failure_retains_partial_and_resumes_without_success(self):
        result = self.run_backup(FAILURE="validation")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Backup validation failed.", result.stderr)
        self.assertNotIn("Created ", result.stdout)
        self.assertTrue((self.output / ".fixture.tar.gz.partial").exists())
        self.assertFalse((self.output / "fixture.tar.gz").exists())
        self.assert_resumed()

    def test_existing_partial_is_never_overwritten(self):
        self.output.mkdir(mode=0o700)
        partial = self.output / ".fixture.tar.gz.partial"
        partial.write_bytes(b"retained previous private attempt")
        partial.chmod(0o640)
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(partial.read_bytes(), b"retained previous private attempt")
        self.assertEqual(stat.S_IMODE(partial.stat().st_mode), 0o640)
        self.assertFalse((self.output / "fixture.tar.gz").exists())
        self.assertNotIn("Created ", result.stdout)
        self.assertFalse(self.calls.exists())

    def test_existing_archive_is_never_overwritten_or_writers_stopped(self):
        self.output.mkdir(mode=0o700)
        archive = self.output / "fixture.tar.gz"
        archive.write_bytes(b"retained completed private backup")
        archive.chmod(0o640)
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(archive.read_bytes(), b"retained completed private backup")
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o640)
        self.assertFalse(self.calls.exists())

    def test_existing_archive_symlinks_are_retained_before_stopping_writers(self):
        self.output.mkdir(mode=0o700)
        for name, dangling in (("existing-link", False), ("dangling-link", True)):
            with self.subTest(dangling=dangling):
                target = self.root / name
                if not dangling:
                    target.write_bytes(b"retained link target")
                    target.chmod(0o640)
                archive = self.output / (name + ".tar.gz")
                archive.symlink_to(target)
                before = archive.lstat()
                result = subprocess.run(
                    ["sh", "/source/deploy/scripts/backup.sh", archive.name],
                    env=self.env, capture_output=True, text=True, timeout=15,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(archive.is_symlink())
                self.assertEqual(os.readlink(archive), str(target))
                self.assertEqual(archive.lstat(), before)
                if not dangling:
                    self.assertEqual(target.read_bytes(), b"retained link target")
                    self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
                self.assertFalse(self.calls.exists())

    def test_existing_partial_symlinks_and_fifo_are_retained_without_running_docker(self):
        self.output.mkdir(mode=0o700)
        for name, kind in (("link", "link"), ("dangling", "dangling"), ("fifo", "fifo")):
            with self.subTest(kind=kind):
                partial = self.output / ("." + name + ".tar.gz.partial")
                target = self.root / name
                if kind == "fifo":
                    os.mkfifo(partial, 0o600)
                else:
                    if kind == "link":
                        target.write_bytes(b"retained partial link target")
                        target.chmod(0o640)
                    partial.symlink_to(target)
                before = partial.lstat()
                result = subprocess.run(
                    ["sh", "/source/deploy/scripts/backup.sh", name + ".tar.gz"],
                    env=self.env, capture_output=True, text=True, timeout=15,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(partial.lstat(), before)
                if kind != "fifo":
                    self.assertEqual(os.readlink(partial), str(target))
                if kind == "link":
                    self.assertEqual(target.read_bytes(), b"retained partial link target")
                    self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
                self.assertFalse(self.calls.exists())

    def test_relative_selected_output_is_canonicalized_for_readonly_validation(self):
        result = self.run_backup(HOMEBOX_BACKUP_DIR="selected-output")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.deploy / "selected-output"
        self.assertTrue((output / "fixture.tar.gz").is_file())
        validation = next(call for call in self.commands() if "tar -tzf" in call[-1])
        self.assertIn(str(output) + ":/backups:ro", validation)

    def test_base_external_and_lan_overlay_choices_are_preserved(self):
        for external, lan in ((0, 0), (1, 0), (1, 1)):
            with self.subTest(external=external, lan=lan):
                output = self.deploy / ("backup-" + str(external) + str(lan))
                overlays = (
                    (",/synthetic/deploy/docker-compose.external-ai.yml" if external else "") +
                    (",/synthetic/deploy/docker-compose.lan.yml" if lan else "")
                )
                result = self.run_backup(
                    HOMEBOX_BACKUP_DIR=str(output), OVERLAYS=overlays,
                    HOMEBOX_BACKUP_EXTERNAL_AI=str(external),
                    HOMEBOX_BACKUP_LAN_HTTPS=str(lan),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                with tarfile.open(output / "fixture.tar.gz") as archive:
                    self.assertEqual(archive.extractfile("deployment-options.env").read(),
                                     f"EXTERNAL_AI={external}\nLAN_HTTPS={lan}\n".encode())
                for call in self.commands():
                    if call[0] == "compose":
                        self.assertEqual("docker-compose.external-ai.yml" in call, bool(external))
                        self.assertEqual("docker-compose.lan.yml" in call, bool(lan))
                self.calls.write_text("")


if __name__ == "__main__":
    unittest.main()
