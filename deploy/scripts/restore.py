"""Restore a trusted offline snapshot into empty, separately mounted destinations."""

import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile
import uuid


ROOT = Path("/restore")
VOLUME_ROOTS = {"homebox", "assistant", "proxy-data", "proxy-config"}
CONFIG_FILES = {"config.env", "docker-compose.yml", "Caddyfile"}
HOST_UID = int(os.environ["RESTORE_UID"])
HOST_GID = int(os.environ["RESTORE_GID"])


def destination(member):
    path = PurePosixPath(member.name)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("Unsafe archive path")
    top = path.parts[0]
    if top in VOLUME_ROOTS or top == "secrets":
        return ROOT.joinpath(*path.parts), top
    if len(path.parts) == 1 and top in CONFIG_FILES and member.isfile():
        return ROOT / "config" / top, "config"
    raise ValueError("Unexpected archive member")


def restore(archive):
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        destinations = set()
        for member in members:
            if not (member.isdir() or member.isfile()):
                raise ValueError("Only plain files and directories are accepted")
            target, _ = destination(member)
            if target in destinations:
                raise ValueError("Duplicate archive path")
            destinations.add(target)
        required = VOLUME_ROOTS | {"secrets"}
        if not required.issubset({PurePosixPath(m.name).parts[0] for m in members}):
            raise ValueError("Snapshot is missing required data")
        if not CONFIG_FILES.issubset({m.name for m in members}):
            raise ValueError("Snapshot is missing its configuration")
        with source.extractfile("config.env") as config:
            identities = [
                line.split("=", 1)[1].strip().strip("\"'")
                for line in config.read().decode().splitlines()
                if line.startswith("HOMEBOX_INSTANCE_ID=")
            ]
        if len(identities) != 1 or uuid.UUID(identities[0]).int == 0:
            raise ValueError("Snapshot needs one non-nil HOMEBOX_INSTANCE_ID")
        for name in VOLUME_ROOTS | {"secrets", "config"}:
            target = ROOT / name
            if target.exists() and (target.is_symlink() or not target.is_dir() or any(target.iterdir())):
                raise ValueError("Refusing to overwrite a non-empty restore destination")
        for name in VOLUME_ROOTS | {"secrets", "config"}:
            (ROOT / name).mkdir(parents=True, exist_ok=True)
        for member in members:
            target, top = destination(member)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as incoming, target.open("xb") as output:
                shutil.copyfileobj(incoming, output)
            os.chmod(target, member.mode & 0o777)
            os.chown(target, member.uid if top in VOLUME_ROOTS else HOST_UID,
                     member.gid if top in VOLUME_ROOTS else HOST_GID)
        # Apply directory metadata last, after writing through restrictive dirs.
        for member in reversed(members):
            if member.isdir():
                target, top = destination(member)
                os.chmod(target, member.mode & 0o777)
                os.chown(target, member.uid if top in VOLUME_ROOTS else HOST_UID,
                         member.gid if top in VOLUME_ROOTS else HOST_GID)
        for name in ("secrets", "config"):
            os.chmod(ROOT / name, 0o700)
            os.chown(ROOT / name, HOST_UID, HOST_GID)
        print(f"Restored {len(members)} entries into empty destinations. Applications remain stopped.")


if __name__ == "__main__":
    try:
        restore(sys.argv[1])
    except (OSError, ValueError, tarfile.TarError, IndexError) as error:
        print(f"Restore failed: {error}", file=sys.stderr)
        sys.exit(1)
