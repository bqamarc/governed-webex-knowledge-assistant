#!/usr/bin/env python3
"""Fail closed when a public release contains private markers or unsafe artifacts."""

from __future__ import annotations

import argparse
import json
import re
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from webex_knowledge_assistant.manifest import KnowledgeManifest

IGNORED_DIRECTORIES = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "htmlcov",
}
RUNTIME_DIRECTORIES = {"data", "tmp", "var"}
IGNORED_FILENAMES = {".coverage"}
BLOCKED_SUFFIXES = {
    ".db",
    ".key",
    ".log",
    ".p12",
    ".pem",
    ".sqlite",
    ".sqlite3",
    ".token",
}
BLOCKED_FILENAMES = {
    ".env",
    "credentials.json",
    "id_dsa",
    "id_ed25519",
    "id_rsa",
}
ALLOWED_URL_HOSTS = {
    "127.0.0.1",
    "developer.webex.com",
    "example.com",
    "github.com",
    "localhost",
    "webexapis.com",
    "www.apache.org",
}
SECRET_PATTERNS = {
    "GitHub token": re.compile(rb"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}"),
    "AWS access key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "private key": re.compile(rb"-----BEGIN (?:EC |OPENSSH |RSA )?PRIVATE KEY-----"),
    "bearer credential": re.compile(
        rb"Authorization:\s*Bearer\s+[A-Za-z0-9._~-]{16,}", re.IGNORECASE
    ),
}
PRIVACY_PATTERNS = {
    "local macOS home path": re.compile(rb"/Users/[A-Za-z0-9._-]+/"),
    "local Windows home path": re.compile(rb"[A-Za-z]:\\Users\\[A-Za-z0-9._ -]+\\"),
    "private URI scheme": re.compile(rb"\b(?:internal|intranet)://", re.IGNORECASE),
}
EMAIL_RE = re.compile(rb"\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b", re.IGNORECASE)
ALLOWED_EMAIL_DOMAINS = {b"example.com", b"users.noreply.github.com"}
URL_RE = re.compile(r"https?://[^\s<>)\]\"']+")
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


class ReleaseCheckError(RuntimeError):
    pass


def candidate_files(root: Path) -> list[Path]:
    files: set[Path] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        if not path.is_file():
            continue
        relative_parts = path.relative_to(root).parts
        if path.name in IGNORED_FILENAMES:
            continue
        if any(part in IGNORED_DIRECTORIES for part in relative_parts):
            continue
        if any(part.endswith(".egg-info") for part in relative_parts):
            continue
        files.add(path)
    if (root / ".git").exists():
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
        )
        for raw_name in result.stdout.split(b"\0"):
            if not raw_name:
                continue
            tracked = root / raw_name.decode("utf-8", errors="surrogateescape")
            if not tracked.is_symlink() and tracked.is_file():
                files.add(tracked)
    return sorted(files)


def scan_bytes(
    label: str,
    content: bytes,
    errors: list[str],
    denylist: tuple[bytes, ...] = (),
) -> None:
    lower = content.lower()
    for marker in denylist:
        if marker.lower() in lower:
            errors.append(f"{label}: contains an external denylist marker")
    for description, pattern in SECRET_PATTERNS.items():
        if pattern.search(content):
            errors.append(f"{label}: contains a {description}")
    for description, pattern in PRIVACY_PATTERNS.items():
        if pattern.search(content):
            errors.append(f"{label}: contains a {description}")
    for match in EMAIL_RE.finditer(content):
        if match.group(1).lower() not in ALLOWED_EMAIL_DOMAINS:
            errors.append(f"{label}: contains a non-public email identity")
            break
    if b"\x00" not in content:
        scan_urls(label, content.decode("utf-8", errors="replace"), errors)


def scan_urls(label: str, text: str, errors: list[str]) -> None:
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:")
        hostname = (urlsplit(url).hostname or "").casefold()
        if hostname not in ALLOWED_URL_HOSTS:
            errors.append(f"{label}: URL host is not allowlisted")


def scan_file(
    root: Path,
    path: Path,
    errors: list[str],
    denylist: tuple[bytes, ...],
) -> None:
    if path.name in BLOCKED_FILENAMES:
        errors.append("Working tree contains a blocked runtime or credential filename")
        return
    if path.suffix.lower() in BLOCKED_SUFFIXES:
        errors.append("Working tree contains a blocked runtime or secret-file suffix")
        return
    if path.stat().st_size > 5 * 1024 * 1024:
        errors.append("Working tree contains a file exceeding the 5 MiB public-release limit")
        return
    content = path.read_bytes()
    scan_bytes("Working tree file content", content, errors, denylist)


def _validate_archive_member_path(
    name: str,
    *,
    is_directory: bool,
    errors: list[str],
    denylist: tuple[bytes, ...],
) -> bool:
    scan_bytes(
        "Archive member path metadata",
        name.encode("utf-8", errors="surrogateescape"),
        errors,
        denylist,
    )
    candidate = name[:-1] if is_directory and name.endswith("/") else name
    raw_parts = candidate.split("/")
    if (
        not candidate
        or candidate.startswith("/")
        or "\\" in candidate
        or re.match(r"^[A-Za-z]:", candidate)
        or any(not part or part in {".", ".."} for part in raw_parts)
        or any(
            any(ord(character) < 32 or ord(character) == 127 for character in part)
            for part in raw_parts
        )
    ):
        errors.append("Distribution archive contains an unsafe member path")
        return False
    path = PurePosixPath(candidate)
    if path.name in BLOCKED_FILENAMES:
        errors.append("Distribution archive contains a blocked runtime or credential filename")
        return False
    if path.suffix.lower() in BLOCKED_SUFFIXES:
        errors.append("Distribution archive contains a blocked runtime or secret-file suffix")
        return False
    return True


def _scan_archive_content(
    name: str,
    content: bytes,
    errors: list[str],
    denylist: tuple[bytes, ...],
) -> None:
    scan_bytes("Archive member content", content, errors, denylist)
    if PurePosixPath(name).suffix.lower() != ".json":
        return
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        errors.append("Distribution archive contains invalid JSON")
        return
    validate_manifest_value("Distribution archive manifest", value, errors)


def scan_archives(root: Path, errors: list[str], denylist: tuple[bytes, ...]) -> None:
    dist = root / "dist"
    if dist.is_symlink():
        errors.append("Distribution directory is a symbolic link")
        return
    if not dist.is_dir():
        return
    for archive in sorted(dist.iterdir()):
        if archive.is_symlink():
            errors.append("Distribution directory contains a symbolic link")
            continue
        if archive.suffix == ".whl" or archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as package:
                for member in package.infolist():
                    if not _validate_archive_member_path(
                        member.filename,
                        is_directory=member.is_dir(),
                        errors=errors,
                        denylist=denylist,
                    ):
                        continue
                    if member.is_dir():
                        continue
                    member_type = (member.external_attr >> 16) & 0o170000
                    if member_type == stat.S_IFLNK:
                        errors.append("Distribution archive contains a symbolic link")
                        continue
                    if member.file_size > 5 * 1024 * 1024:
                        errors.append(
                            "Distribution archive contains a member exceeding the 5 MiB limit"
                        )
                        continue
                    _scan_archive_content(
                        member.filename,
                        package.read(member),
                        errors,
                        denylist,
                    )
        elif archive.name.endswith((".tar.gz", ".tgz")):
            with tarfile.open(archive, "r:gz") as package:
                for member in package.getmembers():
                    if not _validate_archive_member_path(
                        member.name,
                        is_directory=member.isdir(),
                        errors=errors,
                        denylist=denylist,
                    ):
                        continue
                    if member.issym() or member.islnk():
                        errors.append("Distribution archive contains a link")
                        continue
                    if not member.isfile():
                        continue
                    if member.size > 5 * 1024 * 1024:
                        errors.append(
                            "Distribution archive contains a member exceeding the 5 MiB limit"
                        )
                        continue
                    extracted = package.extractfile(member)
                    if extracted is not None:
                        _scan_archive_content(
                            member.name,
                            extracted.read(),
                            errors,
                            denylist,
                        )


def validate_manifest_value(label: str, value: object, errors: list[str]) -> None:
    if not isinstance(value, dict) or "source" not in value or "records" not in value:
        return
    try:
        manifest = KnowledgeManifest.model_validate(value)
    except ValueError:
        errors.append(f"{label}: manifest schema rejected")
        return
    if manifest.source.source_class not in {"public", "synthetic"}:
        errors.append(f"{label}: source class is not public or synthetic")
    if manifest.source.audience != "public" or manifest.source.reviewed is not True:
        errors.append(f"{label}: source is not reviewed for public use")
    for index, record in enumerate(manifest.records):
        if record.answerable and record.citation_mode not in {"url", "title_only"}:
            errors.append(f"{label}: record {index} has unsafe citation policy")


def validate_manifests(root: Path, errors: list[str]) -> None:
    for path in sorted(root.rglob("*.json")):
        if path.is_symlink():
            continue
        if any(part in IGNORED_DIRECTORIES for part in path.relative_to(root).parts):
            continue
        label = "Working tree manifest"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            if "manifest" in path.name.casefold():
                errors.append(f"{label}: invalid JSON")
            continue
        validate_manifest_value(label, value, errors)


def validate_local_markdown_links(root: Path, errors: list[str]) -> None:
    for path in sorted(root.rglob("*.md")):
        if path.is_symlink():
            continue
        if any(part in IGNORED_DIRECTORIES for part in path.relative_to(root).parts):
            continue
        text = path.read_text(encoding="utf-8")
        for raw_target in MARKDOWN_LINK_RE.findall(text):
            target = raw_target.strip().split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            destination = (path.parent / target).resolve()
            try:
                destination.relative_to(root.resolve())
            except ValueError:
                errors.append("Markdown contains a local link that escapes the repository")
                continue
            if not destination.exists():
                errors.append("Markdown contains a missing local link target")


def validate_git_metadata(root: Path, errors: list[str]) -> None:
    if not (root / ".git").exists():
        return
    result = subprocess.run(
        ["git", "log", "--all", "--format=%ae%n%ce"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if "does not have any commits yet" in result.stderr or "unknown revision" in result.stderr:
            return
        errors.append("Git history could not be inspected")
        return
    for email in {line.strip().casefold() for line in result.stdout.splitlines() if line.strip()}:
        if not (email.endswith("@users.noreply.github.com") or email.endswith("@example.com")):
            errors.append("Git history contains an email outside the public identity policy")
            break


def validate_symlinks(
    root: Path,
    errors: list[str],
    denylist: tuple[bytes, ...],
) -> None:
    for path in root.rglob("*"):
        if not path.is_symlink():
            continue
        relative_parts = path.relative_to(root).parts
        if any(part in IGNORED_DIRECTORIES for part in relative_parts):
            continue
        scan_bytes(
            "Working tree path metadata",
            path.relative_to(root).as_posix().encode("utf-8", errors="surrogateescape"),
            errors,
            denylist,
        )
        errors.append("Working tree contains a symbolic link")
        break

    if not (root / ".git").exists():
        return
    result = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        errors.append("Git index topology could not be inspected")
        return
    for entry in result.stdout.split(b"\0"):
        if not entry:
            continue
        try:
            raw_metadata, raw_path = entry.split(b"\t", 1)
        except ValueError:
            errors.append("Git index contains an unreadable entry")
            return
        metadata = raw_metadata.split()
        if not metadata:
            errors.append("Git index contains an unreadable entry")
            return
        scan_bytes("Git index path metadata", raw_path, errors, denylist)
        if metadata[0] == b"120000":
            errors.append("Git index contains a symbolic link")
            return
        if metadata[0] not in {b"100644", b"100755"}:
            errors.append("Git index contains unsupported file topology")
            return


def _validate_history_path(
    path: str,
    errors: list[str],
    denylist: tuple[bytes, ...],
) -> None:
    candidate = PurePosixPath(path)
    scan_bytes(
        "Git history path metadata",
        path.encode("utf-8", errors="surrogateescape"),
        errors,
        denylist,
    )
    if candidate.name in BLOCKED_FILENAMES:
        errors.append("Git history contains a blocked runtime or credential filename")
    if candidate.suffix.lower() in BLOCKED_SUFFIXES:
        errors.append("Git history contains a blocked runtime or secret-file suffix")


def _read_git_object(
    root: Path,
    object_type: str,
    oid: str,
    errors: list[str],
) -> bytes | None:
    size_result = subprocess.run(
        ["git", "cat-file", "-s", oid],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        size = int(size_result.stdout.strip())
    except ValueError:
        errors.append("A reachable Git object size could not be inspected")
        return None
    if size_result.returncode != 0:
        errors.append("A reachable Git object size could not be inspected")
        return None
    if size > 5 * 1024 * 1024:
        errors.append("Git history contains an object exceeding the 5 MiB release limit")
        return None
    content = subprocess.run(
        ["git", "cat-file", object_type, oid],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if content.returncode != 0:
        errors.append("A reachable Git object could not be inspected")
        return None
    return content.stdout


def validate_git_history(
    root: Path,
    errors: list[str],
    denylist: tuple[bytes, ...],
) -> None:
    if not (root / ".git").exists():
        return
    revisions = subprocess.run(
        ["git", "rev-list", "--all"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if revisions.returncode != 0:
        errors.append("Reachable Git history could not be inspected")
        return
    blob_cache: dict[str, bytes | None] = {}
    for revision in revisions.stdout.splitlines():
        commit = _read_git_object(root, "commit", revision, errors)
        if commit is not None:
            scan_bytes("Git commit metadata and message", commit, errors, denylist)
        tree = subprocess.run(
            ["git", "ls-tree", "-r", "-z", revision],
            cwd=root,
            check=False,
            capture_output=True,
        )
        if tree.returncode != 0:
            errors.append("A reachable Git tree could not be inspected")
            return
        for entry in tree.stdout.split(b"\0"):
            if not entry:
                continue
            try:
                metadata, raw_path = entry.split(b"\t", 1)
                mode, object_type, raw_oid = metadata.split()
                path = raw_path.decode("utf-8", errors="surrogateescape")
                oid = raw_oid.decode("ascii")
            except (UnicodeDecodeError, ValueError):
                errors.append("Reachable Git history contains an unreadable entry")
                return
            _validate_history_path(path, errors, denylist)
            if mode == b"120000":
                errors.append("Git history contains a symbolic link")
                continue
            if object_type != b"blob" or mode not in {b"100644", b"100755"}:
                errors.append("Git history contains unsupported file topology")
                continue
            if oid not in blob_cache:
                blob_cache[oid] = _read_git_object(root, "blob", oid, errors)
                if blob_cache[oid] is not None:
                    scan_bytes(
                        f"Git history blob {oid[:12]}",
                        blob_cache[oid] or b"",
                        errors,
                        denylist,
                    )
            blob = blob_cache[oid]
            historical_path = PurePosixPath(path)
            if blob is not None and historical_path.suffix.lower() == ".json":
                try:
                    value = json.loads(blob.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    if "manifest" in historical_path.name.casefold():
                        errors.append(f"Git history manifest blob {oid[:12]}: invalid JSON")
                    continue
                validate_manifest_value(f"Git history manifest blob {oid[:12]}", value, errors)

    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(objecttype) %(objectname) %(refname)"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if refs.returncode != 0:
        errors.append("Git reference metadata could not be inspected")
        return
    for line in refs.stdout.splitlines():
        try:
            object_type, oid, refname = line.split(" ", 2)
        except ValueError:
            errors.append("Git reference metadata contains an unreadable entry")
            return
        scan_bytes("Git reference name", refname.encode(), errors, denylist)
        if object_type not in {"commit", "tag"}:
            errors.append("Git reference has unsupported object topology")
            continue
        if object_type != "tag":
            continue
        tag = _read_git_object(root, "tag", oid, errors)
        if tag is not None:
            scan_bytes("Git tag metadata and message", tag, errors, denylist)


def verify(
    root: Path,
    *,
    denylist: tuple[bytes, ...] = (),
    require_license: bool = False,
) -> None:
    errors: list[str] = []
    if not (root / ".git").exists():
        for directory in sorted(RUNTIME_DIRECTORIES):
            if (root / directory).exists():
                errors.append(
                    f"{directory}/: unexpected runtime directory in a history-free candidate"
                )
    if require_license and not any(
        (root / name).is_file() for name in ("LICENSE", "LICENSE.md", "LICENSE.txt")
    ):
        errors.append("approved LICENSE file is required for publication")
    for path in candidate_files(root):
        scan_bytes(
            "Working tree path metadata",
            path.relative_to(root).as_posix().encode("utf-8", errors="surrogateescape"),
            errors,
            denylist,
        )
        scan_file(root, path, errors, denylist)
    scan_archives(root, errors, denylist)
    validate_manifests(root, errors)
    validate_local_markdown_links(root, errors)
    validate_symlinks(root, errors, denylist)
    validate_git_metadata(root, errors)
    validate_git_history(root, errors, denylist)
    if errors:
        formatted = "\n".join(f"- {error}" for error in sorted(set(errors)))
        raise ReleaseCheckError(f"Public-release verification failed:\n{formatted}")


def load_external_denylist(root: Path, path: Path) -> tuple[bytes, ...]:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        pass
    else:
        raise ReleaseCheckError("The denylist must be stored outside the candidate repository")
    try:
        return tuple(
            line.strip().encode("utf-8")
            for line in resolved.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    except OSError as exc:
        raise ReleaseCheckError(f"Could not read denylist: {exc}") from exc


def main() -> int:
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("root", nargs="?", default=".")
    argument_parser.add_argument(
        "--denylist",
        type=Path,
        help="Optional untracked file with one case-insensitive blocked marker per line",
    )
    argument_parser.add_argument(
        "--require-license",
        action="store_true",
        help="Fail unless an approved LICENSE file is present",
    )
    args = argument_parser.parse_args()
    root = Path(args.root).resolve()
    try:
        denylist = load_external_denylist(root, args.denylist) if args.denylist is not None else ()
        verify(root, denylist=denylist, require_license=args.require_license)
    except ReleaseCheckError as exc:
        print(exc)
        return 1
    print(f"Public-release verification passed for {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
