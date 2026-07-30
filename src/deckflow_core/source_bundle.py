"""Validate Parse Bundles and atomically assemble canonical Luna Source Bundles.

This module is deliberately deterministic. It copies bytes, validates hashes
and references, scrubs runtime provenance, computes the canonical fingerprint,
and swaps a fully validated sibling directory into place. It does not call a
model, infer task intent, or translate content.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__

SOURCE_SCHEMA_VERSION = 1
PARSE_SCHEMA_VERSIONS = {2}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BCP47_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_ID_RE = re.compile(r"^(?P<prefix>[a-z][a-z0-9-]*)-(?P<number>[0-9]+)$")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_EMBEDDED_WINDOWS_PATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s,;)'\"\]]+")
_EMBEDDED_UNIX_PATH_RE = re.compile(r"(?:(?<=^)|(?<=[\s=(:'\"]))/[^/\s][^\s,;)'\"\]]*")

_SOURCE_KINDS = {
    "pdf",
    "docx",
    "doc",
    "rtf",
    "odt",
    "epub",
    "pages",
    "md",
    "txt",
    "pptx",
    "ppt",
    "key",
    "odp",
    "xlsx",
    "xls",
    "csv",
    "ods",
    "numbers",
    "html",
    "image",
    "svg",
    "heic",
    "json",
    "yaml",
}
_LOCATOR_PROFILES = {
    "heading|paragraph|line",
    "heading|paragraph|line|page",
    "heading|paragraph|line|slide",
    "heading|paragraph|line|slide|shape|bbox-emu",
}
_FORBIDDEN_PROVENANCE_KEYS = {
    "origin",
    "bundle",
    "manifest",
    "commands",
    "command",
    "argv",
    "credential",
    "credentials",
    "token",
    "api_key",
    "apiKey",
    "cloud_response",
    "raw_response",
    "sidecar",
    "config_file",
}
_SECRET_ENV_KEYS = (
    "DECKFLOW_API_KEY",
    "DECKOPS_API_KEY",
    "DECKFLOW_TOKEN",
    "DECKOPS_TOKEN",
)
_ACQUISITION_PROVENANCE_KEYS = {
    "requested",
    "status",
    "capability",
    "candidate_engine",
    "selected_engine",
    "size_mb",
    "installed_size_mb",
    "expected_fidelity",
    "cached",
    "error",
}


@dataclass
class SourceBundleError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fail(code: str, message: str) -> SourceBundleError:
    return SourceBundleError(code, message)


def _read_object(path: Path, *, code: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise _fail(code, f"invalid JSON file {path.name}: {error}") from error
    if not isinstance(payload, dict):
        raise _fail(code, f"JSON root must be an object: {path.name}")
    return payload


def _regular_file(path: Path, *, code: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise _fail(code, f"required file is missing: {path}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _fail(code, f"path must be a direct regular file: {path}")
    if info.st_nlink != 1:
        raise _fail(code, f"hard-linked files are not accepted: {path}")
    return info


def require_direct_input(path: Path) -> Path:
    expanded = path.expanduser()
    _regular_file(expanded, code="source-input-invalid")
    return expanded.resolve()


def _relative_file(root: Path, value: Any, *, code: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise _fail(code, "bundle path must be a non-empty POSIX relative path")
    relative = Path(value)
    if relative.is_absolute() or "." in relative.parts or ".." in relative.parts:
        raise _fail(code, f"bundle path escapes its root: {value}")
    target = root / relative
    _regular_file(target, code=code)
    root_real = root.resolve()
    target_real = target.resolve()
    if root_real not in target_real.parents:
        raise _fail(code, f"bundle path escapes its root: {value}")
    return target_real


def _direct_directory(root: Path, value: Any, *, code: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise _fail(code, "bundle directory must be a non-empty POSIX relative path")
    relative = Path(value)
    if relative.is_absolute() or "." in relative.parts or ".." in relative.parts:
        raise _fail(code, f"bundle directory escapes its root: {value}")
    target = root / relative
    try:
        info = target.lstat()
    except OSError as error:
        raise _fail(code, f"required directory is missing: {value}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _fail(code, f"bundle directory must be direct: {value}")
    target_real = target.resolve()
    if root.resolve() not in target_real.parents:
        raise _fail(code, f"bundle directory escapes its root: {value}")
    return target_real


def _assert_bundle_tree(root: Path, *, code: str) -> None:
    try:
        info = root.lstat()
    except OSError as error:
        raise _fail(code, f"bundle root is missing: {root}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _fail(code, f"bundle root must be a direct directory: {root}")
    for directory, names, files in os.walk(root, followlinks=False):
        for name in [*names, *files]:
            path = Path(directory) / name
            child = path.lstat()
            if stat.S_ISLNK(child.st_mode):
                raise _fail(code, f"symlink is not accepted: {path.relative_to(root)}")
            if stat.S_ISREG(child.st_mode) and child.st_nlink != 1:
                raise _fail(code, f"hard link is not accepted: {path.relative_to(root)}")


def _entry_ids(entries: Iterable[dict[str, Any]], field: str, *, code: str) -> list[str]:
    values = [entry.get(field) for entry in entries]
    if any(not isinstance(value, str) or not value for value in values):
        raise _fail(code, f"invalid {field}")
    if len(values) != len(set(values)):
        raise _fail(code, f"duplicate {field}")
    return [str(value) for value in values]


def _next_id(entries: Iterable[dict[str, Any]], field: str, prefix: str) -> str:
    highest = 0
    for value in (entry.get(field) for entry in entries):
        if not isinstance(value, str):
            continue
        match = _ID_RE.match(value)
        if match and value.startswith(f"{prefix}-"):
            highest = max(highest, int(match.group("number")))
    return f"{prefix}-{highest + 1:03d}"


def _safe_name(name: str, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).name).strip("-.") or "source"
    stem = Path(base).stem or "source"
    suffix = Path(base).suffix.lower()
    candidate = f"{stem}{suffix}"
    counter = 2
    while candidate.lower() in used:
        candidate = f"{stem}-{counter}{suffix}"
        counter += 1
    used.add(candidate.lower())
    return candidate


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _indexed_paths(manifest: dict[str, Any]) -> list[str]:
    paths: set[str] = set()
    for key in ("content", "visual", "target"):
        value = manifest.get(key)
        if isinstance(value, str):
            paths.add(value)
    for collection in ("sources", "materials", "assets"):
        for entry in manifest.get(collection) or ():
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                paths.add(entry["path"])
    return sorted(paths)


def canonical_source_fingerprint(manifest: dict[str, Any], bundle: Path) -> str:
    projection = {
        key: value
        for key, value in manifest.items()
        if key not in {"status", "content_fingerprint", "review"}
    }
    files = [
        {
            "path": path,
            "sha256": sha256_file(
                _relative_file(bundle, path, code="source-indexed-file-invalid")
            ),
        }
        for path in _indexed_paths(manifest)
    ]
    return hashlib.sha256(
        _canonical_json({"manifest": projection, "files": files})
    ).hexdigest()


def _validate_sha(entry: dict[str, Any], path: Path, *, code: str) -> None:
    expected = entry.get("sha256")
    if not isinstance(expected, str) or not _SHA256_RE.match(expected):
        raise _fail(code, f"invalid sha256 for {path.name}")
    if sha256_file(path) != expected:
        raise _fail(code, f"sha256 mismatch for {path.name}")


def _coverage(value: Any, *, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(code, "coverage must be an object")
    counts = [value.get(key) for key in ("expected", "included", "omitted")]
    if any(not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in counts):
        raise _fail(code, "coverage counts must be non-negative integers")
    if counts[0] != counts[1] + counts[2]:
        raise _fail(code, "coverage counts do not close")
    if value.get("claim") != "bounded":
        raise _fail(code, "coverage claim must be bounded")
    return value


def _scrub_string(value: str, secrets: Iterable[str]) -> str:
    cleaned = value
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "<redacted>")
    if cleaned.startswith("/") or _WINDOWS_ABSOLUTE_RE.match(cleaned):
        return "<redacted-path>"
    cleaned = _EMBEDDED_WINDOWS_PATH_RE.sub("<redacted-path>", cleaned)
    cleaned = _EMBEDDED_UNIX_PATH_RE.sub("<redacted-path>", cleaned)
    return cleaned


def scrub_provenance(value: Any, *, secrets: Iterable[str] = ()) -> Any:
    if isinstance(value, dict):
        return {
            key: scrub_provenance(child, secrets=secrets)
            for key, child in value.items()
            if key not in _FORBIDDEN_PROVENANCE_KEYS
        }
    if isinstance(value, list):
        return [scrub_provenance(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        return _scrub_string(value, secrets)
    return value


def _scrub_acquisition(value: dict[str, Any], *, secrets: Iterable[str]) -> dict[str, Any]:
    return {
        key: scrub_provenance(child, secrets=secrets)
        for key, child in value.items()
        if key in _ACQUISITION_PROVENANCE_KEYS
    }


def _contains_runtime_provenance(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key in _FORBIDDEN_PROVENANCE_KEYS or _contains_runtime_provenance(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_runtime_provenance(item) for item in value)
    if isinstance(value, str):
        return (
            value.startswith("/")
            or bool(_WINDOWS_ABSOLUTE_RE.match(value))
            or bool(_EMBEDDED_WINDOWS_PATH_RE.search(value))
            or bool(_EMBEDDED_UNIX_PATH_RE.search(value))
        )
    return False


def _parse_fingerprint(manifest: dict[str, Any], bundle: Path) -> str:
    document = _relative_file(
        bundle,
        manifest["outputs"]["document"],
        code="parse-document-invalid",
    )
    entries = [(manifest["outputs"]["document"], sha256_file(document))]
    for asset in manifest["assets"]:
        entries.append((asset["path"], asset["sha256"]))
    lines = sorted(f"{path}:{digest}" for path, digest in entries)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def validate_parse_bundle(
    bundle: Path,
    input_path: Path,
    provider_result: dict[str, Any],
) -> dict[str, Any]:
    """Validate provider stdout and disk output as one trust boundary."""
    _assert_bundle_tree(bundle, code="parse-bundle-invalid")
    manifest_path = _relative_file(
        bundle,
        "parse-manifest.json",
        code="parse-manifest-invalid",
    )

    for field, expected in (("bundle", bundle), ("manifest", manifest_path)):
        value = provider_result.get(field)
        if not isinstance(value, str) or Path(value).expanduser().resolve() != expected.resolve():
            raise _fail(
                "parse-report-path-mismatch",
                f"provider_result.{field} does not match the transient Parse Bundle",
            )

    manifest = _read_object(manifest_path, code="parse-manifest-invalid")
    if manifest.get("schema_version") not in PARSE_SCHEMA_VERSIONS:
        raise _fail("parse-schema-unsupported", "unsupported Parse Bundle schema")
    tool = manifest.get("tool")
    if (
        not isinstance(tool, dict)
        or tool.get("name") != "deckflow-extract"
        or not isinstance(tool.get("version"), str)
    ):
        raise _fail("parse-provider-invalid", "unexpected or incomplete parse provider")
    if manifest.get("mode") not in {"local", "cloud"}:
        raise _fail("parse-mode-invalid", "parse mode must be local or cloud")

    input_record = manifest.get("input")
    if not isinstance(input_record, dict):
        raise _fail("parse-input-invalid", "Parse Bundle input record is missing")
    input_digest = sha256_file(input_path)
    if input_record.get("sha256") != input_digest:
        raise _fail("parse-input-hash-mismatch", "Parse Bundle input hash does not match")
    if input_record.get("type") not in _SOURCE_KINDS:
        raise _fail("parse-input-invalid", "Parse Bundle input type is not controlled")

    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        raise _fail("parse-output-invalid", "Parse Bundle outputs are missing")
    _relative_file(bundle, outputs.get("document"), code="parse-document-invalid")
    if outputs.get("assets_dir") != "assets":
        raise _fail("parse-assets-invalid", "Parse Bundle assets_dir must be assets")
    _direct_directory(bundle, "assets", code="parse-assets-invalid")

    document = manifest.get("document")
    if not isinstance(document, dict):
        raise _fail("parse-document-invalid", "Parse Bundle document metadata is missing")
    if document.get("locator_profile") not in _LOCATOR_PROFILES:
        raise _fail("parse-locator-profile-invalid", "unsupported document locator profile")
    if not isinstance(document.get("language"), str) or not document["language"]:
        raise _fail("parse-source-language-invalid", "source language is missing")

    for field, expected_type in (
        ("fidelity", dict),
        ("gaps", list),
        ("decision", dict),
        ("recommendations", list),
        ("assets", list),
        ("diagnostics", list),
    ):
        if not isinstance(manifest.get(field), expected_type):
            raise _fail("parse-manifest-invalid", f"manifest.{field} has the wrong type")
    _coverage(manifest.get("coverage"), code="parse-coverage-invalid")

    asset_ids = _entry_ids(manifest["assets"], "asset_id", code="parse-asset-invalid")
    if len(asset_ids) != len(manifest["assets"]):
        raise _fail("parse-asset-invalid", "invalid Parse Bundle asset IDs")
    for asset in manifest["assets"]:
        path = _relative_file(bundle, asset.get("path"), code="parse-asset-invalid")
        _validate_sha(asset, path, code="parse-asset-hash-mismatch")
        locator = asset.get("locator")
        if locator is not None and (not isinstance(locator, str) or not locator):
            raise _fail("parse-asset-invalid", "asset locator must be a non-empty string")

    if manifest["decision"].get("usable") is not True:
        raise _fail("parse-result-unusable", "parse decision is not usable")
    if any(
        isinstance(gap, dict) and gap.get("severity") == "blocking"
        for gap in manifest["gaps"]
    ):
        raise _fail("parse-result-blocking", "Parse Bundle contains a blocking gap")

    provider_status = provider_result.get("status")
    if provider_status not in {"parsed", "repairable"}:
        raise _fail("parse-report-status-invalid", "provider status is not importable")
    for field in ("tier", "fidelity", "gaps", "decision", "recommendations"):
        if provider_result.get(field) != manifest.get(field):
            raise _fail(
                "parse-report-manifest-mismatch",
                f"provider_result.{field} disagrees with parse-manifest.json",
            )
    report_coverage = provider_result.get("coverage")
    if (
        not isinstance(report_coverage, dict)
        or report_coverage.get("included") != manifest["coverage"].get("included")
        or report_coverage.get("omitted") != manifest["coverage"].get("omitted")
    ):
        raise _fail(
            "parse-report-manifest-mismatch",
            "provider_result.coverage disagrees with parse-manifest.json",
        )
    if provider_result.get("engine_acquisition") != manifest.get("engine_acquisition"):
        raise _fail(
            "parse-report-manifest-mismatch",
            "engine acquisition outcome disagrees with parse-manifest.json",
        )

    fingerprint = manifest.get("content_fingerprint")
    if (
        not isinstance(fingerprint, str)
        or not _SHA256_RE.match(fingerprint)
        or _parse_fingerprint(manifest, bundle) != fingerprint
    ):
        raise _fail("parse-fingerprint-mismatch", "Parse Bundle fingerprint does not verify")
    return manifest


def validate_source_bundle(bundle: Path) -> dict[str, Any]:
    """Validate every canonical index, hash, reference and provenance field."""
    _assert_bundle_tree(bundle, code="source-bundle-invalid")
    manifest_path = _relative_file(bundle, "manifest.json", code="source-manifest-invalid")
    manifest = _read_object(manifest_path, code="source-manifest-invalid")
    if manifest.get("schema_version") != SOURCE_SCHEMA_VERSION:
        raise _fail("source-schema-unsupported", "unsupported Source Bundle schema")
    if manifest.get("bundle_id") != "source-main" or manifest.get("content") != "content.json":
        raise _fail("source-manifest-invalid", "invalid bundle ID or content entrypoint")
    if manifest.get("status") not in {"draft", "review-ready", "confirmed"}:
        raise _fail("source-manifest-invalid", "invalid Source Bundle status")

    collections: dict[str, list[dict[str, Any]]] = {}
    for field in ("sources", "materials", "assets", "imports", "diagnostics"):
        value = manifest.get(field)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise _fail("source-manifest-invalid", f"manifest.{field} must be an object array")
        collections[field] = value

    source_ids = set(
        _entry_ids(collections["sources"], "source_id", code="source-id-invalid")
    )
    material_ids = set(
        _entry_ids(collections["materials"], "material_id", code="source-id-invalid")
    )
    _entry_ids(collections["assets"], "asset_id", code="source-id-invalid")
    _entry_ids(collections["imports"], "import_id", code="source-id-invalid")

    for source in collections["sources"]:
        path = _relative_file(bundle, source.get("path"), code="source-file-invalid")
        _validate_sha(source, path, code="source-hash-mismatch")
        if source.get("kind") not in _SOURCE_KINDS:
            raise _fail("source-entry-invalid", "source kind is not controlled")
        if not isinstance(source.get("include"), bool):
            raise _fail("source-entry-invalid", "source include must be boolean")

    material_by_id: dict[str, dict[str, Any]] = {}
    for material in collections["materials"]:
        path = _relative_file(bundle, material.get("path"), code="source-material-invalid")
        _validate_sha(material, path, code="source-hash-mismatch")
        if material.get("type") not in {"raw", "structure"}:
            raise _fail("source-material-invalid", "material type must be raw or structure")
        refs = material.get("source_refs")
        if (
            not isinstance(refs, list)
            or not refs
            or any(ref not in source_ids for ref in refs)
        ):
            raise _fail("source-material-invalid", "material source_refs do not resolve")
        material_by_id[material["material_id"]] = material

    for asset in collections["assets"]:
        path = _relative_file(bundle, asset.get("path"), code="source-asset-invalid")
        _validate_sha(asset, path, code="source-hash-mismatch")
        if asset.get("source_ref") not in source_ids:
            raise _fail("source-asset-invalid", "asset source_ref does not resolve")
        if not isinstance(asset.get("locator"), str) or not asset["locator"]:
            raise _fail("source-asset-invalid", "asset locator is required")
        if asset.get("role") not in {"unassigned", "evidence", "decoration", "background"}:
            raise _fail("source-asset-invalid", "invalid asset role")
        if asset.get("origin") not in {
            "source-extracted",
            "project-found",
            "searched",
            "ai-generated",
        }:
            raise _fail("source-asset-invalid", "invalid asset origin")
        if asset.get("style_mode") not in {
            "native-authentic",
            "theme-aligned",
            "theme-neutral",
        }:
            raise _fail("source-asset-invalid", "invalid asset style mode")

    for imported in collections["imports"]:
        if imported.get("source_ref") not in source_ids:
            raise _fail("source-import-invalid", "import source_ref does not resolve")
        for field in ("provider", "parser", "engine", "fidelity", "decision"):
            if not isinstance(imported.get(field), dict):
                raise _fail("source-import-invalid", f"import {field} must be an object")
        for field in ("provider", "parser"):
            identity = imported[field]
            if (
                not isinstance(identity.get("name"), str)
                or not identity["name"]
                or not isinstance(identity.get("version"), str)
                or not identity["version"]
            ):
                raise _fail("source-import-invalid", f"import {field} identity is incomplete")
        if (
            not isinstance(imported.get("parse_manifest_sha256"), str)
            or not _SHA256_RE.match(imported["parse_manifest_sha256"])
        ):
            raise _fail("source-import-invalid", "import Parse manifest hash is invalid")
        if imported.get("mode") not in {"local", "cloud"}:
            raise _fail("source-import-invalid", "import mode must be local or cloud")
        if (
            not isinstance(imported.get("source_language"), str)
            or not imported["source_language"]
        ):
            raise _fail("source-import-invalid", "import source language is missing")
        _coverage(imported.get("coverage"), code="source-import-invalid")
        if not isinstance(imported.get("gaps"), list) or not isinstance(
            imported.get("recommendations"), list
        ):
            raise _fail(
                "source-import-invalid",
                "import gaps and recommendations must be arrays",
            )
        if imported["decision"].get("usable") is not True or any(
            isinstance(gap, dict) and gap.get("severity") == "blocking"
            for gap in imported["gaps"]
        ):
            raise _fail("source-import-invalid", "imported parse result is not usable")
        if "engine_acquisition" in imported and not isinstance(
            imported["engine_acquisition"], dict
        ):
            raise _fail("source-import-invalid", "engine acquisition must be an object")

    for diagnostic in collections["diagnostics"]:
        if diagnostic.get("source_ref") not in source_ids:
            raise _fail("source-diagnostic-invalid", "diagnostic source_ref does not resolve")
        if (
            not isinstance(diagnostic.get("code"), str)
            or not diagnostic["code"]
            or diagnostic.get("severity") not in {"info", "warning", "error"}
            or not isinstance(diagnostic.get("message"), str)
            or not diagnostic["message"]
        ):
            raise _fail("source-diagnostic-invalid", "diagnostic fields are incomplete")
        if "recovery" in diagnostic and not isinstance(diagnostic["recovery"], str):
            raise _fail("source-diagnostic-invalid", "diagnostic recovery must be text")

    content = _read_object(
        _relative_file(bundle, "content.json", code="source-content-invalid"),
        code="source-content-invalid",
    )
    if content.get("schema_version") != 1:
        raise _fail("source-content-invalid", "unsupported content schema")
    if not isinstance(content.get("brief"), str) or not content["brief"].strip():
        raise _fail("source-content-invalid", "content brief is required")
    language = content.get("language")
    if not isinstance(language, str) or not _BCP47_RE.match(language):
        raise _fail("source-content-invalid", "content language must be a BCP 47 tag")
    content_materials = content.get("materials")
    if (
        not isinstance(content_materials, list)
        or any(not isinstance(item, dict) for item in content_materials)
    ):
        raise _fail("source-content-invalid", "content materials must be an object array")
    seen_materials: set[str] = set()
    for material in content_materials:
        material_id = material.get("material_id")
        indexed = material_by_id.get(material_id)
        if indexed is None or material.get("path") != indexed.get("path"):
            raise _fail("source-content-invalid", "content material is not indexed")
        if material_id in seen_materials:
            raise _fail("source-content-invalid", "duplicate content material")
        if material.get("locator_profile") not in _LOCATOR_PROFILES:
            raise _fail("source-content-invalid", "unsupported content locator profile")
        if material.get("kind") not in _SOURCE_KINDS:
            raise _fail("source-content-invalid", "content material kind is not controlled")
        seen_materials.add(str(material_id))
    if seen_materials != material_ids:
        raise _fail("source-content-invalid", "content material whitelist is incomplete")

    for optional in ("visual", "target"):
        if optional in manifest:
            _relative_file(bundle, manifest[optional], code=f"source-{optional}-invalid")

    coverage = _coverage(manifest.get("coverage"), code="source-coverage-invalid")
    expected = len(collections["sources"])
    included = sum(1 for source in collections["sources"] if source.get("include") is True)
    if (
        coverage.get("expected") != expected
        or coverage.get("included") != included
        or coverage.get("omitted") != expected - included
    ):
        raise _fail("source-coverage-invalid", "Source Bundle coverage does not match sources")

    if _contains_runtime_provenance(collections["imports"]) or _contains_runtime_provenance(
        collections["diagnostics"]
    ):
        raise _fail(
            "source-provenance-not-scrubbed",
            "canonical provenance contains a runtime path or forbidden field",
        )

    fingerprint = manifest.get("content_fingerprint")
    if (
        not isinstance(fingerprint, str)
        or not _SHA256_RE.match(fingerprint)
        or canonical_source_fingerprint(manifest, bundle) != fingerprint
    ):
        raise _fail("source-fingerprint-mismatch", "Source Bundle fingerprint does not verify")
    return manifest


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _atomic_commit(staging: Path, target: Path) -> None:
    backup = Path(tempfile.mkdtemp(prefix=".source-bundle-backup.", dir=target.parent))
    backup.rmdir()
    moved_old = False
    try:
        if target.exists():
            os.replace(target, backup)
            moved_old = True
        os.replace(staging, target)
    except BaseException:
        if moved_old and backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)


def assemble_source_bundle(
    *,
    project: Path,
    input_path: Path,
    parse_bundle: Path,
    provider_result: dict[str, Any],
    brief: str,
    deck_language: str,
    title: str | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Validate, assemble, validate again, then atomically commit."""
    project_raw = project.expanduser()
    try:
        project_info = project_raw.lstat()
    except OSError as error:
        raise _fail("source-project-invalid", "project directory does not exist") from error
    if stat.S_ISLNK(project_info.st_mode) or not stat.S_ISDIR(project_info.st_mode):
        raise _fail("source-project-invalid", "project must be a direct directory")
    project = project_raw.resolve()
    input_path = require_direct_input(input_path)
    brief = brief.strip()
    if not brief:
        raise _fail("source-brief-invalid", "brief is required")
    if not _BCP47_RE.match(deck_language):
        raise _fail("source-language-invalid", "deck language must be a BCP 47 tag")

    parse_manifest = validate_parse_bundle(parse_bundle, input_path, provider_result)
    target = project / "source-bundle"
    existing: dict[str, Any] | None = None
    if target.exists():
        existing = validate_source_bundle(target)
        if existing.get("status") == "confirmed":
            raise _fail(
                "source-bundle-confirmed",
                "confirmed Source Bundles cannot be changed by deckflow parse",
            )
    previous_fingerprint = existing.get("content_fingerprint") if existing else None

    staging = Path(tempfile.mkdtemp(prefix=".source-bundle.", dir=project))
    try:
        if existing is not None and not replace:
            shutil.copytree(target, staging, dirs_exist_ok=True)
            manifest = _read_object(
                staging / "manifest.json",
                code="source-manifest-invalid",
            )
            content = _read_object(
                staging / "content.json",
                code="source-content-invalid",
            )
        else:
            for directory in ("src", "materials", "assets"):
                (staging / directory).mkdir(parents=True, exist_ok=True)
            manifest = {
                "schema_version": SOURCE_SCHEMA_VERSION,
                "bundle_id": "source-main",
                "title": title or "Project sources",
                "content": "content.json",
                "sources": [],
                "materials": [],
                "assets": [],
                "imports": [],
                "diagnostics": [],
                "coverage": {},
                "status": "review-ready",
                "content_fingerprint": "pending",
            }
            content = {
                "schema_version": 1,
                "brief": brief,
                "deck_type": "standard",
                "language": deck_language,
                "materials": [],
            }

        sources: list[dict[str, Any]] = manifest["sources"]
        materials: list[dict[str, Any]] = manifest["materials"]
        assets: list[dict[str, Any]] = manifest["assets"]
        imports: list[dict[str, Any]] = manifest["imports"]
        diagnostics: list[dict[str, Any]] = manifest["diagnostics"]

        input_digest = sha256_file(input_path)
        if any(
            source.get("sha256") == input_digest and source.get("include") is True
            for source in sources
        ):
            raise _fail(
                "source-already-included",
                "the same source bytes are already included in this project",
            )

        source_id = _next_id(sources, "source_id", "source")
        material_id = _next_id(materials, "material_id", "material")
        import_id = _next_id(imports, "import_id", "parse-import")
        used_names = {
            Path(str(source.get("path") or "")).name.lower() for source in sources
        }
        source_name = _safe_name(input_path.name, used_names)
        copied_source = staging / "src" / source_name
        copied_source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(input_path, copied_source)
        if sha256_file(copied_source) != input_digest or sha256_file(input_path) != input_digest:
            raise _fail("source-input-changed", "input changed while it was being imported")

        parse_input = parse_manifest["input"]
        sources.append(
            {
                "source_id": source_id,
                "path": f"src/{source_name}",
                "kind": parse_input["type"],
                "media_type": parse_input.get("media_type") or "application/octet-stream",
                "sha256": input_digest,
                "include": True,
                "scope": "whole file",
            }
        )

        parse_document = _relative_file(
            parse_bundle,
            parse_manifest["outputs"]["document"],
            code="parse-document-invalid",
        )
        material_relative = f"materials/{source_id}.md"
        material_target = staging / material_relative
        material_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(parse_document, material_target)
        materials.append(
            {
                "material_id": material_id,
                "path": material_relative,
                "type": "raw",
                "source_refs": [source_id],
                "sha256": sha256_file(material_target),
            }
        )
        content["materials"].append(
            {
                "material_id": material_id,
                "kind": parse_input["type"],
                "path": material_relative,
                "locator_profile": parse_manifest["document"]["locator_profile"],
            }
        )

        digest_paths = {
            asset["sha256"]: asset["path"]
            for asset in assets
            if isinstance(asset.get("sha256"), str) and isinstance(asset.get("path"), str)
        }
        for parse_asset in parse_manifest["assets"]:
            asset_id = _next_id(assets, "asset_id", "asset")
            digest = parse_asset["sha256"]
            destination_relative = digest_paths.get(digest)
            parse_asset_path = _relative_file(
                parse_bundle,
                parse_asset["path"],
                code="parse-asset-invalid",
            )
            if destination_relative is None:
                suffix = parse_asset_path.suffix.lower()
                if not re.match(r"^\.[A-Za-z0-9]{1,8}$", suffix):
                    suffix = ".bin"
                destination_relative = f"assets/{asset_id}-{digest[:12]}{suffix}"
                destination = staging / destination_relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(parse_asset_path, destination)
                if sha256_file(destination) != digest:
                    raise _fail("source-asset-copy-mismatch", "asset changed while being copied")
                digest_paths[digest] = destination_relative
            assets.append(
                {
                    "asset_id": asset_id,
                    "path": destination_relative,
                    "sha256": digest,
                    "source_ref": source_id,
                    "locator": parse_asset.get("locator") or "whole-file",
                    "role": "unassigned",
                    "origin": "source-extracted",
                    "evidence_authority": "source",
                    "style_mode": "native-authentic",
                    "usage_rights": "user-supplied",
                    "selection": {
                        "priority": "core-source",
                        "classification": "pending",
                    },
                }
            )

        secrets = [
            str(input_path),
            str(parse_bundle),
            str(project),
            str(target),
            str(parse_manifest.get("input", {}).get("origin") or ""),
            *[os.environ.get(key, "") for key in _SECRET_ENV_KEYS],
        ]
        import_record: dict[str, Any] = {
            "import_id": import_id,
            "source_ref": source_id,
            "provider": {"name": "deckflow-core", "version": __version__},
            "parser": scrub_provenance(parse_manifest["tool"], secrets=secrets),
            "engine": scrub_provenance(parse_manifest.get("engine") or {}, secrets=secrets),
            "parse_manifest_sha256": sha256_file(parse_bundle / "parse-manifest.json"),
            "mode": parse_manifest["mode"],
            "source_language": parse_manifest["document"]["language"],
            "fidelity": scrub_provenance(parse_manifest["fidelity"], secrets=secrets),
            "coverage": scrub_provenance(parse_manifest["coverage"], secrets=secrets),
            "gaps": scrub_provenance(parse_manifest["gaps"], secrets=secrets),
            "decision": scrub_provenance(parse_manifest["decision"], secrets=secrets),
            "recommendations": scrub_provenance(
                parse_manifest["recommendations"],
                secrets=secrets,
            ),
        }
        if parse_manifest.get("engine_acquisition"):
            import_record["engine_acquisition"] = _scrub_acquisition(
                parse_manifest["engine_acquisition"],
                secrets=secrets,
            )
        imports.append(import_record)

        for diagnostic in parse_manifest["diagnostics"]:
            cleaned = scrub_provenance(diagnostic, secrets=secrets)
            if not isinstance(cleaned, dict):
                continue
            diagnostics.append(
                {
                    "code": str(cleaned.get("code") or "parse-diagnostic"),
                    "severity": (
                        cleaned.get("severity")
                        if cleaned.get("severity") in {"info", "warning", "error"}
                        else "info"
                    ),
                    "source_ref": source_id,
                    "message": str(cleaned.get("message") or "parse diagnostic"),
                    **(
                        {"locator": cleaned["locator"]}
                        if isinstance(cleaned.get("locator"), str) and cleaned["locator"]
                        else {}
                    ),
                }
            )

        content["brief"] = brief
        content["language"] = deck_language
        if title:
            manifest["title"] = title
        manifest["coverage"] = {
            "claim": "bounded",
            "expected": len(sources),
            "included": sum(1 for source in sources if source.get("include") is True),
            "omitted": sum(1 for source in sources if source.get("include") is False),
            "notes": [],
        }
        manifest["status"] = (
            "draft"
            if existing is not None and not replace and existing.get("status") == "draft"
            else "review-ready"
        )
        _write_json(staging / "content.json", content)
        manifest["content_fingerprint"] = "pending"
        manifest["content_fingerprint"] = canonical_source_fingerprint(manifest, staging)
        _write_json(staging / "manifest.json", manifest)
        validate_source_bundle(staging)
        current_fingerprint = manifest["content_fingerprint"]
        _atomic_commit(staging, target)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise

    return {
        "bundle": str(target),
        "manifest": str(target / "manifest.json"),
        "source_id": source_id,
        "material_id": material_id,
        "import_id": import_id,
        "previous_fingerprint": previous_fingerprint,
        "content_fingerprint": current_fingerprint,
    }


__all__ = [
    "SourceBundleError",
    "assemble_source_bundle",
    "canonical_source_fingerprint",
    "require_direct_input",
    "scrub_provenance",
    "sha256_file",
    "validate_parse_bundle",
    "validate_source_bundle",
]
