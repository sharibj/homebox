"""Create a source-only overlay after every conversational candidate is final."""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import zipfile


ROOTS = ("apps/assistant-api", "apps/pwa", "services/homebox-mcp", "deploy", "docs/assistant")
PRIVATE = {
    "secrets", "certs", "certificates", "backups", "private", "runtime", "data",
    "uploads", "qa-output", "qa-results", "pki", "caddy_data", "caddy_config",
    ".auth", "auth", "checkpoints", ".checkpoints", "trace", "traces",
    "logs", "tmp", "temp",
}
PRIVATE_NAMES = {".npmrc", ".netrc", ".pypirc", "credentials.json", "qa-credentials.json",
                 "storage-state.json", "storagestate.json", "auth.json", "runtime.compose.json",
                 "checkpoint.json", "trace.json", "trace.zip"}
GENERATED = {
    "node_modules", "dist", "build", "coverage", "htmlcov", "test-results",
    "playwright-report", "blob-report", "screenshots", "artifacts", "__pycache__",
    ".git", ".factory", ".venv", "venv", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".cache", ".vite",
}
SOURCE_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".vue", ".json", ".yaml",
    ".yml", ".toml", ".lock", ".txt", ".md", ".sh", ".conf", ".html", ".css",
    ".svg", ".webmanifest", ".ini", ".in",
}
PUBLIC_ICONS = {"apps/pwa/public/icon-192.png", "apps/pwa/public/icon-512.png"}
SPECIAL_NAMES = {".gitignore", ".dockerignore", ".prettierrc", ".prettierignore",
                 ".eslintignore", "Makefile"}
REQUIRED = {
    "apps/assistant-api/assistant/main.py", "apps/pwa/src/App.vue",
    "services/homebox-mcp/adapter/main.py", "deploy/docker-compose.yml",
    "deploy/.env.example", "docs/assistant/deployment.md",
    "deploy/docker-compose.external-ai.yml", "deploy/docker-compose.lan.yml",
    "deploy/reverse-proxy/Caddyfile", "deploy/reverse-proxy/Caddyfile.lan",
    "apps/pwa/tests/browser/conversation-live.spec.mjs",
}
POLICY = {
    "includedRoots": list(ROOTS),
    "excludedPrivateDirectories": sorted(PRIVATE),
    "excludedPrivateFilenames": sorted(PRIVATE_NAMES),
    "excludedGeneratedDirectories": sorted(GENERATED),
    "environment": "Exclude .env and .env.*; only deploy/.env.example is included.",
    "binaryFiles": "Only the two named public PWA icons are included.",
    "otherFiles": "Include recognized source/configuration formats only.",
    "links": "Reject unexcluded symlinks instead of following them.",
}


def exclusion(relative):
    lowered = {part.lower() for part in relative.parts}
    if lowered & PRIVATE:
        return "private-directory"
    if lowered & GENERATED or any(part.lower().endswith(".egg-info") for part in relative.parts):
        return "generated-directory"
    if relative.name.lower() in PRIVATE_NAMES:
        return "private-filename"
    if relative.name.lower() == ".env" or relative.name.lower().startswith(".env."):
        return None if relative.as_posix() == "deploy/.env.example" else "environment-file"
    return None


def source_file(relative):
    return (
        relative.as_posix() == "deploy/.env.example"
        or relative.as_posix() in PUBLIC_ICONS
        or relative.suffix in SOURCE_SUFFIXES
        or relative.name in SPECIAL_NAMES
        or relative.name.startswith(("Dockerfile", "Caddyfile"))
    )


def collect(root):
    selected, skipped = [], Counter()
    total_bytes = 0
    for owned in ROOTS:
        directory = root / owned
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Every owned source root must be a real directory")
        for parent, directories, files in os.walk(directory, followlinks=False):
            keep = []
            for name in sorted(directories):
                path = Path(parent) / name
                reason = exclusion(path.relative_to(root))
                if reason:
                    skipped[reason] += 1
                elif path.is_symlink():
                    raise ValueError("Refusing a source-directory symlink")
                else:
                    keep.append(name)
            directories[:] = keep
            for name in sorted(files):
                path = Path(parent) / name
                relative = path.relative_to(root)
                reason = exclusion(relative)
                if reason:
                    skipped[reason] += 1
                    continue
                if path.is_symlink():
                    raise ValueError("Refusing a source-file symlink")
                if not path.is_file():
                    raise ValueError("Refusing a non-regular source file")
                if not source_file(relative):
                    skipped["non-source-format"] += 1
                    continue
                if path.stat().st_size > 8 * 1024**2:
                    raise ValueError("Source file exceeds the archive safety limit")
                content = path.read_bytes()
                total_bytes += len(content)
                if total_bytes > 32 * 1024**2:
                    raise ValueError("Source overlay exceeds the archive safety limit")
                if relative.as_posix() not in PUBLIC_ICONS:
                    content.decode("utf-8")
                selected.append((relative.as_posix(), content, stat.S_IMODE(path.stat().st_mode)))
    if not REQUIRED <= {name for name, _, _ in selected}:
        raise ValueError("Required source/configuration files are missing")
    return sorted(selected), dict(sorted(skipped.items()))


def build(root, output, release, base_revision, images, approved):
    if not approved:
        raise ValueError("Final candidate approval is required before creating the release archive")
    if not root.is_absolute() or not output.is_absolute():
        raise ValueError("Use absolute repository and output paths")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,100}", release):
        raise ValueError("Invalid release label")
    if not re.fullmatch(r"[0-9a-f]{40}", base_revision):
        raise ValueError("A full base revision is required")
    if set(images) != {"api", "mcp", "pwa", "homebox"} or not all(
        re.fullmatch(r"sha256:[0-9a-f]{64}", image) for image in images.values()
    ):
        raise ValueError("Four actual immutable application image IDs are required")
    if not output.name.startswith("conversational-release-source-") or output.suffix != ".zip":
        raise ValueError("Use a new conversational-release-source-*.zip filename")
    if output.exists():
        raise FileExistsError("Never replace an existing source artifact")
    resolved_root, resolved_output = root.resolve(), output.resolve()
    if any(resolved_output.is_relative_to(resolved_root / owned) for owned in ROOTS):
        raise ValueError("Keep archive output outside the included source roots")
    selected, skipped = collect(resolved_root)
    manifest = {
        "format": "homebox-conversational-source-overlay-v1",
        "release": release, "baseRevision": base_revision, "applicationImages": images,
        "policy": POLICY, "excludedCounts": skipped,
        "files": [{"path": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
                  for name, content, _ in selected],
    }
    readme = (
        "# Conversational release source overlay\n\n"
        f"Release: `{release}`. Base checkout: `{base_revision}`.\n\n"
        "This ZIP contains the owned app, MCP, deployment, and assistant-documentation source. "
        "It is an overlay, not a full Homebox checkout or a runtime/data backup. Apply it to "
        "the recorded base checkout before following docs/assistant/deployment.md.\n\n"
        "SOURCE-MANIFEST.json records the four actual application image IDs and SHA-256/size "
        "of every included source file. Source hashes describe exactly the archived bytes.\n\n"
        "Excluded: environment values, secrets, keys/certificates, backups, inventory/uploads, "
        "private runtime/test outputs, node_modules, dist, builds, caches, and unrecognized "
        "binary files. The neutral deploy/.env.example and two public PWA icons are included. "
        "Unexcluded symlinks are rejected. No private excluded filenames are listed.\n"
    )
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content, mode in selected:
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = ((stat.S_IFREG | mode) & 0xFFFF) << 16
            archive.writestr(info, content)
        archive.writestr("SOURCE-MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
        archive.writestr("SOURCE-README.md", readme)
    return {"sourceFiles": len(selected), "sourceBytes": sum(len(data) for _, data, _ in selected),
            "archiveBytes": output.stat().st_size,
            "archiveSHA256": hashlib.sha256(output.read_bytes()).hexdigest(), "excludedCounts": skipped}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--base-revision", required=True)
    for role in ("api", "mcp", "pwa", "homebox"):
        parser.add_argument(f"--{role}-image", required=True)
    parser.add_argument("--final-candidates-approved", action="store_true")
    args = parser.parse_args()
    images = {role: getattr(args, role + "_image") for role in ("api", "mcp", "pwa", "homebox")}
    result = build(args.root, args.output, args.release, args.base_revision,
                   images, args.final_candidates_approved)
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
