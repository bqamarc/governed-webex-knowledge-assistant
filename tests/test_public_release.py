from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.verify_public_release import (
    ReleaseCheckError,
    candidate_files,
    load_external_denylist,
    verify,
)


def test_publication_gate_requires_a_license(tmp_path: Path) -> None:
    with pytest.raises(ReleaseCheckError, match="approved LICENSE"):
        verify(tmp_path, require_license=True)

    (tmp_path / "LICENSE").write_text("Synthetic approved license.\n", encoding="utf-8")
    verify(tmp_path, require_license=True)


def test_denylist_must_remain_outside_candidate_root(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    inside = candidate / "denylist.txt"
    inside.write_text("private-marker\n", encoding="utf-8")
    with pytest.raises(ReleaseCheckError, match="outside"):
        load_external_denylist(candidate, inside)

    outside = tmp_path / "external-denylist.txt"
    outside.write_text("private-marker\n", encoding="utf-8")
    assert load_external_denylist(candidate, outside) == (b"private-marker",)


def test_history_free_candidate_rejects_runtime_directories(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "innocent.txt").write_text("synthetic\n", encoding="utf-8")
    with pytest.raises(ReleaseCheckError, match="unexpected runtime directory"):
        verify(tmp_path)


def test_force_tracked_files_cannot_hide_in_ignored_directories(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    hidden = tmp_path / "data" / "tracked.txt"
    hidden.parent.mkdir()
    hidden.write_text("/" + "Users/example/private.txt\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", "data/tracked.txt"], cwd=tmp_path, check=True)

    assert hidden in candidate_files(tmp_path)
    with pytest.raises(ReleaseCheckError, match="local macOS home path"):
        verify(tmp_path)


@pytest.mark.parametrize("runtime_directory", ["data", "tmp", "var"])
def test_force_tracked_runtime_manifest_receives_policy_validation(
    tmp_path: Path,
    runtime_directory: str,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    manifest = tmp_path / runtime_directory / "restricted-manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": {
                    "id": "restricted-example",
                    "title": "Restricted Example",
                    "uri": "urn:example:restricted",
                    "source_class": "restricted",
                    "audience": "restricted",
                    "reviewed": True,
                },
                "records": [
                    {
                        "id": "audit-record",
                        "question": "Is this public?",
                        "keywords": ["public"],
                        "answer": "No.",
                        "answerable": False,
                        "citation_mode": "none",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "-f", str(manifest.relative_to(tmp_path))],
        cwd=tmp_path,
        check=True,
    )

    with pytest.raises(ReleaseCheckError, match="source class is not public or synthetic"):
        verify(tmp_path)


def test_force_tracked_external_symlink_is_rejected_without_following(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=candidate, check=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("benign external content\n", encoding="utf-8")
    link = candidate / "external-link.txt"
    link.symlink_to(outside)
    subprocess.run(["git", "add", "external-link.txt"], cwd=candidate, check=True)

    with pytest.raises(ReleaseCheckError, match="symbolic link"):
        verify(candidate)


def test_deleted_sensitive_content_in_reachable_history_is_rejected(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Example Maintainer"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "maintainer@example.com"], cwd=tmp_path, check=True
    )
    historical = tmp_path / "deleted.txt"
    historical.write_text("/" + "Users/example/private.txt\n", encoding="utf-8")
    subprocess.run(["git", "add", "deleted.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add synthetic fixture"], cwd=tmp_path, check=True)
    historical.unlink()
    subprocess.run(["git", "add", "-u"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "remove synthetic fixture"], cwd=tmp_path, check=True
    )

    with pytest.raises(ReleaseCheckError, match="local macOS home path"):
        verify(tmp_path)


def test_deleted_restricted_manifest_in_reachable_history_is_rejected(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Example Maintainer"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "maintainer@example.com"], cwd=tmp_path, check=True
    )
    historical = tmp_path / "restricted-manifest.json"
    historical.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": {
                    "id": "restricted-history",
                    "title": "Restricted History",
                    "uri": "urn:example:restricted-history",
                    "source_class": "restricted",
                    "audience": "restricted",
                    "reviewed": True,
                },
                "records": [
                    {
                        "id": "historical-record",
                        "question": "Was this public?",
                        "keywords": ["public"],
                        "answer": "No.",
                        "answerable": False,
                        "citation_mode": "none",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", historical.name], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add restricted fixture"], cwd=tmp_path, check=True
    )
    historical.unlink()
    subprocess.run(["git", "add", "-u"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "remove restricted fixture"], cwd=tmp_path, check=True
    )

    with pytest.raises(ReleaseCheckError, match="source class is not public or synthetic"):
        verify(tmp_path)


def test_external_denylist_marker_in_current_filename_is_rejected(tmp_path: Path) -> None:
    marker = "blocked" + "-filename-marker"
    (tmp_path / f"{marker}.txt").write_text("benign content\n", encoding="utf-8")

    with pytest.raises(ReleaseCheckError, match="external denylist marker"):
        verify(tmp_path, denylist=(marker.encode(),))


def test_external_denylist_marker_in_deleted_historical_filename_is_rejected(
    tmp_path: Path,
) -> None:
    marker = "blocked" + "-historical-filename"
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Example Maintainer"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "maintainer@example.com"], cwd=tmp_path, check=True
    )
    historical = tmp_path / f"{marker}.txt"
    historical.write_text("benign content\n", encoding="utf-8")
    subprocess.run(["git", "add", historical.name], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add benign fixture"], cwd=tmp_path, check=True)
    historical.unlink()
    subprocess.run(["git", "add", "-u"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "remove benign fixture"], cwd=tmp_path, check=True)

    with pytest.raises(ReleaseCheckError, match="external denylist marker"):
        verify(tmp_path, denylist=(marker.encode(),))


def test_commit_message_and_committer_email_are_scanned(tmp_path: Path) -> None:
    marker = "blocked" + "-commit-subject"
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Example Maintainer"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "maintainer@example.com"], cwd=tmp_path, check=True
    )
    (tmp_path / "clean.txt").write_text("benign content\n", encoding="utf-8")
    subprocess.run(["git", "add", "clean.txt"], cwd=tmp_path, check=True)
    environment = os.environ.copy()
    environment["GIT_AUTHOR_NAME"] = "Example Author"
    environment["GIT_AUTHOR_EMAIL"] = "author@example.com"
    environment["GIT_COMMITTER_NAME"] = "Private Committer"
    environment["GIT_COMMITTER_EMAIL"] = "private" + "@" + "not-public.invalid"
    subprocess.run(
        ["git", "commit", "-q", "-m", marker],
        cwd=tmp_path,
        check=True,
        env=environment,
    )

    with pytest.raises(ReleaseCheckError) as error:
        verify(tmp_path, denylist=(marker.encode(),))
    assert "external denylist marker" in str(error.value)
    assert "non-public email identity" in str(error.value) or "identity policy" in str(error.value)


def test_annotated_tag_metadata_and_message_are_scanned(tmp_path: Path) -> None:
    marker = "blocked" + "-tag-message"
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Example Maintainer"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "maintainer@example.com"], cwd=tmp_path, check=True
    )
    (tmp_path / "clean.txt").write_text("benign content\n", encoding="utf-8")
    subprocess.run(["git", "add", "clean.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add benign fixture"], cwd=tmp_path, check=True)
    environment = os.environ.copy()
    environment["GIT_COMMITTER_NAME"] = "Private Tagger"
    environment["GIT_COMMITTER_EMAIL"] = "tagger" + "@" + "not-public.invalid"
    tag_message = "\n".join(
        (
            marker,
            "/" + "Users/example/private.txt",
            "https://" + "not-public.invalid/reference",
        )
    )
    subprocess.run(
        ["git", "tag", "-a", "v0.0.1", "-m", tag_message],
        cwd=tmp_path,
        check=True,
        env=environment,
    )

    with pytest.raises(ReleaseCheckError) as error:
        verify(tmp_path, denylist=(marker.encode(),))
    message = str(error.value)
    assert "external denylist marker" in message
    assert "local macOS home path" in message
    assert "URL host is not allowlisted" in message
    assert "non-public email identity" in message


def _restricted_manifest_bytes() -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "source": {
                "id": "restricted-archive",
                "title": "Restricted Archive",
                "uri": "urn:example:restricted-archive",
                "source_class": "restricted",
                "audience": "restricted",
                "reviewed": True,
            },
            "records": [
                {
                    "id": "archive-record",
                    "question": "Is this public?",
                    "keywords": ["public"],
                    "answer": "No.",
                    "answerable": False,
                    "citation_mode": "none",
                }
            ],
        }
    ).encode()


def test_restricted_manifest_with_nonstandard_current_name_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "knowledge.json").write_bytes(_restricted_manifest_bytes())

    with pytest.raises(ReleaseCheckError, match="source class is not public or synthetic"):
        verify(tmp_path)


def test_deleted_restricted_manifest_with_nonstandard_name_is_rejected(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Example Maintainer"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "maintainer@example.com"], cwd=tmp_path, check=True
    )
    historical = tmp_path / "knowledge.json"
    historical.write_bytes(_restricted_manifest_bytes())
    subprocess.run(["git", "add", historical.name], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add knowledge fixture"], cwd=tmp_path, check=True)
    historical.unlink()
    subprocess.run(["git", "add", "-u"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "remove knowledge fixture"], cwd=tmp_path, check=True
    )

    with pytest.raises(ReleaseCheckError, match="source class is not public or synthetic"):
        verify(tmp_path)


@pytest.mark.parametrize("archive_kind", ["zip", "tar"])
def test_restricted_manifest_inside_archive_is_rejected(
    tmp_path: Path,
    archive_kind: str,
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    content = _restricted_manifest_bytes()
    if archive_kind == "zip":
        with zipfile.ZipFile(dist / "candidate.zip", "w") as package:
            package.writestr("package/restricted-manifest.json", content)
    else:
        with tarfile.open(dist / "candidate.tar.gz", "w:gz") as package:
            member = tarfile.TarInfo("package/restricted-manifest.json")
            member.size = len(content)
            package.addfile(member, io.BytesIO(content))

    with pytest.raises(ReleaseCheckError, match="source class is not public or synthetic"):
        verify(tmp_path)


@pytest.mark.parametrize("archive_kind", ["zip", "tar"])
def test_archive_member_traversal_is_rejected(tmp_path: Path, archive_kind: str) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    content = b"benign content\n"
    if archive_kind == "zip":
        with zipfile.ZipFile(dist / "candidate.zip", "w") as package:
            package.writestr("../../escape.txt", content)
    else:
        with tarfile.open(dist / "candidate.tar.gz", "w:gz") as package:
            member = tarfile.TarInfo("../../escape.txt")
            member.size = len(content)
            package.addfile(member, io.BytesIO(content))

    with pytest.raises(ReleaseCheckError, match="unsafe member path"):
        verify(tmp_path)
