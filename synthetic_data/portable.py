"""Rebuild a corpus into portable filenames, archive it, and verify the round trip.

The Phase G pilot contains two workbook gold artifacts whose names embed
``workbook:shareholder_register``.  On Windows that colon made the file an NTFS
alternate data stream attached to a zero-byte base file: the JSON was readable
through the same path, so every validation passed, but the data was invisible to
normal tooling and would be lost by any copy to a non-NTFS filesystem.

This module republishes a corpus under :mod:`synthetic_data.paths` names **without
changing a byte of content** — the canonical ids stay inside the JSON, the file
contents are copied verbatim, and every copied file is checked against the source
manifest's digest.  The source directory is never modified.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synthetic_data.paths import PortablePaths

__all__ = ["PORTABLE_SUFFIX", "RebuildResult", "rebuild_portable", "archive_portable",
           "verify_archive", "manifest_name"]

PORTABLE_SUFFIX = "_portable"
MANIFEST = "manifest_portable.json"
ARCHIVE = "v3_pilot_portable.zip"
ROLES = ("v1", "v2", "alignment")


@dataclass
class RebuildResult:
    root: Path
    files: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    renamed: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def by_role(self, role: str) -> list[dict[str, Any]]:
        return [entry for entry in self.files if entry["role"] == role]

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for entry in self.files:
            counts[entry["role"]] = counts.get(entry["role"], 0) + 1
        return {"files": len(self.files), "by_role": counts,
                "problems": self.problems, "renamed": len(self.renamed)}


def manifest_name() -> str:
    return MANIFEST


def _digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _source_names(artifact: dict[str, Any]) -> dict[str, str]:
    base = artifact["document_id"].replace(":", "_")
    kind = artifact["doc_type"]
    fmt = artifact["format"]
    return {"v1": f"{base}__{kind}__{fmt}.json",
            "v2": f"{base}__{kind}__{fmt}.json",
            "alignment": f"{base}__{fmt}.json"}


def rebuild_portable(source: Path, target: Path) -> RebuildResult:
    """Copy a corpus into ``target`` with portable names; content is unchanged."""
    source = Path(source)
    target = Path(target)
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    result = RebuildResult(root=target)
    paths = PortablePaths()

    if target.exists():
        shutil.rmtree(target)
    for role in ROLES:
        (target / "gold" / role).mkdir(parents=True, exist_ok=True)
    (target / "rendered").mkdir(parents=True, exist_ok=True)
    (target / "fixtures" / "equivalence").mkdir(parents=True, exist_ok=True)

    def publish(src: Path, rel: str, role: str, key: dict[str, Any],
                expected: str | None) -> None:
        if not src.exists():
            result.problems.append(f"missing source {src}")
            return
        data = src.read_bytes()
        if not data:
            result.problems.append(f"empty source {src}")
            return
        digest = hashlib.sha256(data).hexdigest()
        if expected is not None and digest != expected:
            result.problems.append(f"{src} does not match its recorded digest")
        destination = target / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        if destination.stat().st_size != len(data):
            result.problems.append(f"{destination} has the wrong size after writing")
        result.files.append({"role": role, "path": rel, "sha256": digest,
                             "bytes": len(data), **key})

    for artifact in manifest["artifacts"]:
        document_id, doc_type, fmt = (artifact["document_id"], artifact["doc_type"],
                                      artifact["format"])
        for role, old_name in _source_names(artifact).items():
            name = paths.name(document_id, doc_type, fmt, role,
                              directory=f"gold/{role}")
            if name != old_name:
                result.renamed.append({"from": old_name, "to": name, "role": role})
            publish(source / "gold" / role / old_name,
                    f"gold/{role}/{name}", role,
                    {"document_id": document_id, "doc_type": doc_type, "format": fmt,
                     "kind": "gold"}, artifact["gold"][role])

    suffix = {"txt": ".txt", "pdf": ".pdf", "docx": ".docx", "xlsx": ".xlsx"}
    for artifact in manifest["artifacts"]:
        base = artifact["document_id"].replace(":", "_")
        old_name = f"{base}{suffix[artifact['format']]}"
        new_name = f"{base}{suffix[artifact['format']]}"
        publish(source / "rendered" / old_name, f"rendered/{new_name}", "rendered",
                {"document_id": artifact["document_id"], "doc_type": artifact["doc_type"],
                 "format": artifact["format"], "kind": "rendered"},
                artifact.get("render_sha256"))

    equivalence_dir = source / "fixtures" / "equivalence"
    if equivalence_dir.exists():
        for item in sorted(equivalence_dir.iterdir()):
            if item.is_file():
                publish(item, f"fixtures/equivalence/{item.name}", "equivalence",
                        {"document_id": "", "doc_type": "equivalence",
                         "format": item.suffix.lstrip("."), "kind": "equivalence"},
                        None)

    # regular, non-empty files for every role
    for role in ROLES:
        count = len(result.by_role(role))
        expected = len(manifest["artifacts"])
        if count != expected:
            result.problems.append(f"{role}: {count} files, expected {expected}")
    for entry in result.files:
        path = target / entry["path"]
        if not path.is_file():
            result.problems.append(f"{entry['path']} is not a regular file")
        elif path.stat().st_size == 0:
            result.problems.append(f"{entry['path']} is empty")

    manifest_out = {
        "portable_version": 1,
        "source": str(source),
        "canonical_ids_inside_json": True,
        "files": result.files,
        "renamed": result.renamed,
        "problems": result.problems,
        "mapping": {f"{k[0]}|{k[1]}|{k[2]}|{k[3]}": v
                    for k, v in paths.mapping().items()},
    }
    (target / MANIFEST).write_text(
        json.dumps(manifest_out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def archive_portable(root: Path, archive: Path) -> dict[str, Any]:
    """Zip a portable corpus deterministically (sorted, fixed timestamps)."""
    root = Path(root)
    archive = Path(archive)
    if archive.exists():
        archive.unlink()
    entries = sorted(path for path in root.rglob("*") if path.is_file())
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in entries:
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            bundle.writestr(info, path.read_bytes())
    return {"archive": str(archive), "entries": len(entries),
            "sha256": _digest(archive), "bytes": archive.stat().st_size}


def verify_archive(archive: Path, expected: dict[str, str]) -> dict[str, Any]:
    """Extract to a temporary directory and compare names and hashes."""
    archive = Path(archive)
    problems: list[str] = []
    checked = 0
    with tempfile.TemporaryDirectory(prefix="portable_check_") as scratch:
        destination = Path(scratch)
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            for name in names:
                if ":" in name:
                    problems.append(f"{name} contains a colon")
                if name.endswith((" ", ".")):
                    problems.append(f"{name} ends with a space or dot")
            bundle.extractall(destination)
        extracted = {path.relative_to(destination).as_posix(): path
                     for path in destination.rglob("*") if path.is_file()}
        for name, digest in expected.items():
            path = extracted.get(name)
            if path is None:
                problems.append(f"{name} missing after extraction")
                continue
            if path.stat().st_size == 0:
                problems.append(f"{name} is empty after extraction")
                continue
            if _digest(path) != digest:
                problems.append(f"{name} hash differs after extraction")
                continue
            checked += 1
        if MANIFEST not in extracted:
            problems.append(f"{MANIFEST} missing from the archive")
        extra = set(extracted) - set(expected) - {MANIFEST}
        if extra:
            problems.append(f"unexpected entries: {sorted(extra)[:5]}")
    return {"checked": checked, "expected": len(expected), "problems": problems,
            "ok": not problems}
