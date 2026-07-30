"""Canonical Source Bundle trust boundary, mapping, and rollback tests."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deckflow_core import source_bundle as source_bundle_module
from deckflow_core.source_bundle import (
    SourceBundleError,
    assemble_source_bundle,
    canonical_source_fingerprint,
    sha256_file,
    validate_parse_bundle,
    validate_source_bundle,
)

_FIDELITY = {
    "text": "full",
    "structure": "full",
    "tables": "full",
    "images": "full",
    "notes": "none",
    "provenance": "line",
}


def _parse_fingerprint(document: Path, assets: list[dict]) -> str:
    entries = [("document.md", sha256_file(document))]
    entries += [(asset["path"], asset["sha256"]) for asset in assets]
    lines = sorted(f"{path}:{digest}" for path, digest in entries)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def make_parse_bundle(
    root: Path,
    source: Path,
    *,
    markdown: str = "# Parsed\n\nExact value: 12.0M.\n",
    status: str = "parsed",
    gaps: list[dict] | None = None,
    usable: bool = True,
    source_language: str = "en-US",
    acquisition: dict | None = None,
    asset_locators: list[str | None] | None = None,
) -> tuple[Path, dict]:
    bundle = root / "parse-bundle"
    assets_dir = bundle / "assets"
    assets_dir.mkdir(parents=True)
    document = bundle / "document.md"
    document.write_text(markdown, encoding="utf-8")

    assets: list[dict] = []
    shared = b"\x89PNG\r\n\x1a\nsame-image"
    for index, locator in enumerate(asset_locators or [], start=1):
        asset_path = assets_dir / "shared.png"
        asset_path.write_bytes(shared)
        record = {
            "asset_id": f"asset-{index:03d}",
            "path": "assets/shared.png",
            "sha256": hashlib.sha256(shared).hexdigest(),
        }
        if locator is not None:
            record["locator"] = locator
        assets.append(record)

    gaps = list(gaps or [])
    decision = {
        "usable": usable,
        "recommended": "accept" if usable else "input",
        "reason": "fixture",
        "options": ["accept"] if usable else [],
    }
    recommendations = [
        {
            "action": "accept",
            "priority": "high",
            "available": True,
            "gain": [],
            "cost": {"kind": "none"},
            "rerun_required": False,
            "summary": "fixture",
        }
    ] if usable else []
    coverage = {
        "claim": "bounded",
        "expected": 1,
        "included": 1,
        "omitted": 0,
        "unit": "file",
        "notes": [],
    }
    manifest = {
        "schema_version": 2,
        "tool": {"name": "deckflow-extract", "version": "0.3.0"},
        "mode": "local",
        "engine": {"name": "fixture", "ocr": None},
        "input": {
            "type": "md",
            "origin": str(source.resolve()),
            "media_type": "text/markdown",
            "sha256": sha256_file(source),
            "fetched_at": None,
        },
        "outputs": {"document": "document.md", "assets_dir": "assets"},
        "document": {
            "title": "Parsed",
            "language": source_language,
            "locator_profile": "heading|paragraph|line",
        },
        "tier": 0,
        "fidelity": dict(_FIDELITY),
        "element_stats": {"text": 2, "image": len(assets)},
        "assets": assets,
        "coverage": coverage,
        "gaps": gaps,
        "decision": decision,
        "recommendations": recommendations,
        "content_fingerprint": "pending",
        "diagnostics": [],
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    if acquisition is not None:
        manifest["engine_acquisition"] = acquisition
    manifest["content_fingerprint"] = _parse_fingerprint(document, assets)
    manifest_path = bundle / "parse-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    provider = {
        "status": status,
        "bundle": str(bundle.resolve()),
        "manifest": str(manifest_path.resolve()),
        "tier": manifest["tier"],
        "fidelity": manifest["fidelity"],
        "coverage": {"included": 1, "omitted": 0},
        "gaps": gaps,
        "decision": decision,
        "recommendations": recommendations,
        "diagnostics": [],
    }
    if acquisition is not None:
        provider["engine_acquisition"] = acquisition
    return bundle, provider


def assemble(
    project: Path,
    source: Path,
    parse_bundle: Path,
    provider: dict,
    **overrides,
) -> dict:
    options = {
        "project": project,
        "input_path": source,
        "parse_bundle": parse_bundle,
        "provider_result": provider,
        "brief": "Create a management summary",
        "deck_language": "zh-CN",
        "title": None,
        "replace": False,
    }
    options.update(overrides)
    return assemble_source_bundle(**options)


class ParseBundleValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="core-source-test-"))
        self.source = self.root / "input.md"
        self.source.write_text("# Original\n", encoding="utf-8")

    def test_valid_provider_report_and_bundle_close(self) -> None:
        bundle, provider = make_parse_bundle(self.root / "run", self.source)
        manifest = validate_parse_bundle(bundle, self.source, provider)
        self.assertEqual(manifest["input"]["sha256"], sha256_file(self.source))

    def test_input_hash_mismatch_is_rejected(self) -> None:
        bundle, provider = make_parse_bundle(self.root / "run", self.source)
        manifest_path = bundle / "parse-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["input"]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(SourceBundleError, "input hash"):
            validate_parse_bundle(bundle, self.source, provider)

    def test_report_path_mismatch_is_rejected(self) -> None:
        bundle, provider = make_parse_bundle(self.root / "run", self.source)
        provider["bundle"] = str(self.root / "another")
        with self.assertRaisesRegex(SourceBundleError, "provider_result.bundle"):
            validate_parse_bundle(bundle, self.source, provider)

    def test_path_escape_is_rejected(self) -> None:
        bundle, provider = make_parse_bundle(self.root / "run", self.source)
        manifest_path = bundle / "parse-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["outputs"]["document"] = "../outside.md"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(SourceBundleError, "escapes"):
            validate_parse_bundle(bundle, self.source, provider)

    def test_symlink_asset_is_rejected(self) -> None:
        bundle, provider = make_parse_bundle(
            self.root / "run",
            self.source,
            asset_locators=["line:1"],
        )
        asset = bundle / "assets" / "shared.png"
        outside = self.root / "outside.png"
        outside.write_bytes(asset.read_bytes())
        asset.unlink()
        asset.symlink_to(outside)
        with self.assertRaisesRegex(SourceBundleError, "symlink"):
            validate_parse_bundle(bundle, self.source, provider)

    def test_blocking_gap_is_rejected_even_when_status_says_parsed(self) -> None:
        gaps = [{"kind": "text", "severity": "blocking"}]
        bundle, provider = make_parse_bundle(
            self.root / "run",
            self.source,
            gaps=gaps,
        )
        with self.assertRaisesRegex(SourceBundleError, "blocking"):
            validate_parse_bundle(bundle, self.source, provider)

    def test_document_tamper_breaks_parse_fingerprint(self) -> None:
        bundle, provider = make_parse_bundle(self.root / "run", self.source)
        (bundle / "document.md").write_text("# changed\n", encoding="utf-8")
        with self.assertRaisesRegex(SourceBundleError, "fingerprint"):
            validate_parse_bundle(bundle, self.source, provider)


class CanonicalAssemblyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="core-source-test-"))
        self.project = self.root / "project"
        self.project.mkdir()

    def source_and_parse(self, name: str, text: str = "# Original\n", **kwargs):
        source = self.root / name
        source.write_text(text, encoding="utf-8")
        parse_bundle, provider = make_parse_bundle(
            self.root / f"run-{name}",
            source,
            **kwargs,
        )
        return source, parse_bundle, provider

    def test_maps_source_material_language_and_provenance_without_ai(self) -> None:
        source, parse_bundle, provider = self.source_and_parse(
            "input.md",
            source_language="en-US",
            asset_locators=["slide:1;shape:a", "slide:2;shape:b"],
        )
        result = assemble(self.project, source, parse_bundle, provider)

        bundle = self.project / "source-bundle"
        manifest = validate_source_bundle(bundle)
        content = json.loads((bundle / "content.json").read_text())
        self.assertEqual(content["brief"], "Create a management summary")
        self.assertEqual(content["language"], "zh-CN")
        self.assertEqual(manifest["imports"][0]["source_language"], "en-US")
        self.assertEqual(manifest["imports"][0]["provider"]["name"], "deckflow-core")
        self.assertEqual(manifest["imports"][0]["parser"]["name"], "deckflow-extract")
        self.assertEqual(len(manifest["assets"]), 2)
        self.assertEqual(
            [asset["locator"] for asset in manifest["assets"]],
            ["slide:1;shape:a", "slide:2;shape:b"],
        )
        self.assertEqual(manifest["assets"][0]["path"], manifest["assets"][1]["path"])
        self.assertEqual(result["content_fingerprint"], manifest["content_fingerprint"])

    def test_absolute_origin_and_runtime_fields_are_scrubbed(self) -> None:
        source, parse_bundle, provider = self.source_and_parse("input.md")
        manifest_path = parse_bundle / "parse-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        acquisition = {
            "status": "failed",
            "capability": "pptx",
            "sidecar": str(self.root / "secret-sidecar"),
            "error": f"installer failed at {self.root}/private/token",
            "commands": ["deckflow-extract install pptx"],
            "installed_at": "2026-07-30T00:00:00Z",
            "elapsed_ms": 1234,
        }
        manifest["engine_acquisition"] = acquisition
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        provider["status"] = "repairable"
        provider["engine_acquisition"] = acquisition

        assemble(self.project, source, parse_bundle, provider)
        manifest_text = (self.project / "source-bundle" / "manifest.json").read_text()
        self.assertNotIn(str(source.resolve()), manifest_text)
        self.assertNotIn(str(parse_bundle.resolve()), manifest_text)
        self.assertNotIn("secret-sidecar", manifest_text)
        self.assertNotIn('"commands"', manifest_text)
        self.assertNotIn("installed_at", manifest_text)
        self.assertNotIn("elapsed_ms", manifest_text)
        self.assertIn("<redacted-path>", manifest_text)

    def test_appends_to_review_ready_bundle(self) -> None:
        first = self.source_and_parse("first.md", "# First\n")
        assemble(self.project, *first)
        first_fingerprint = validate_source_bundle(
            self.project / "source-bundle"
        )["content_fingerprint"]

        second = self.source_and_parse("second.md", "# Second\n")
        assemble(self.project, *second)
        manifest = validate_source_bundle(self.project / "source-bundle")
        self.assertEqual(len(manifest["sources"]), 2)
        self.assertEqual(len(manifest["materials"]), 2)
        self.assertEqual(len(manifest["imports"]), 2)
        self.assertNotEqual(first_fingerprint, manifest["content_fingerprint"])

    def test_replace_rebuilds_draft_bundle(self) -> None:
        first = self.source_and_parse("first.md", "# First\n")
        assemble(self.project, *first)
        second = self.source_and_parse("second.md", "# Second\n")
        assemble(self.project, *second, replace=True)
        manifest = validate_source_bundle(self.project / "source-bundle")
        self.assertEqual(len(manifest["sources"]), 1)
        self.assertEqual(manifest["sources"][0]["path"], "src/second.md")

    def test_confirmed_bundle_is_immutable(self) -> None:
        first = self.source_and_parse("first.md", "# First\n")
        assemble(self.project, *first)
        manifest_path = self.project / "source-bundle" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["status"] = "confirmed"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        before = manifest_path.read_bytes()

        second = self.source_and_parse("second.md", "# Second\n")
        with self.assertRaisesRegex(SourceBundleError, "confirmed"):
            assemble(self.project, *second, replace=True)
        self.assertEqual(manifest_path.read_bytes(), before)

    def test_duplicate_source_fails_without_changing_existing_bundle(self) -> None:
        first = self.source_and_parse("first.md", "# Same\n")
        assemble(self.project, *first)
        manifest_path = self.project / "source-bundle" / "manifest.json"
        before = manifest_path.read_bytes()

        duplicate_source = self.root / "copy.md"
        duplicate_source.write_text("# Same\n", encoding="utf-8")
        parse_bundle, provider = make_parse_bundle(
            self.root / "run-copy",
            duplicate_source,
        )
        with self.assertRaisesRegex(SourceBundleError, "same source bytes"):
            assemble(self.project, duplicate_source, parse_bundle, provider)
        self.assertEqual(manifest_path.read_bytes(), before)

    def test_partial_fallback_with_no_blocking_gap_is_imported(self) -> None:
        acquisition = {
            "requested": True,
            "status": "failed",
            "capability": "pptx",
            "error": "network unavailable",
        }
        source, parse_bundle, provider = self.source_and_parse(
            "input.md",
            status="repairable",
            acquisition=acquisition,
        )
        assemble(self.project, source, parse_bundle, provider)
        manifest = validate_source_bundle(self.project / "source-bundle")
        self.assertEqual(
            manifest["imports"][0]["engine_acquisition"]["status"],
            "failed",
        )

    def test_final_rename_failure_restores_old_bundle(self) -> None:
        first = self.source_and_parse("first.md", "# First\n")
        assemble(self.project, *first)
        target = self.project / "source-bundle"
        before = validate_source_bundle(target)["content_fingerprint"]
        second = self.source_and_parse("second.md", "# Second\n")
        real_replace = os.replace
        calls = 0

        def flaky_replace(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated final rename failure")
            return real_replace(source, destination)

        with (
            patch.object(source_bundle_module.os, "replace", side_effect=flaky_replace),
            self.assertRaisesRegex(OSError, "simulated"),
        ):
            assemble(self.project, *second)

        self.assertEqual(validate_source_bundle(target)["content_fingerprint"], before)
        self.assertFalse(any(self.project.glob(".source-bundle-backup.*")))

    def test_tampered_existing_bundle_is_never_overwritten(self) -> None:
        first = self.source_and_parse("first.md", "# First\n")
        assemble(self.project, *first)
        target = self.project / "source-bundle"
        (target / "content.json").write_text("{}", encoding="utf-8")
        before = (target / "manifest.json").read_bytes()
        second = self.source_and_parse("second.md", "# Second\n")

        with self.assertRaises(SourceBundleError):
            assemble(self.project, *second, replace=True)
        self.assertEqual((target / "manifest.json").read_bytes(), before)

    def test_import_provenance_must_reference_a_canonical_source(self) -> None:
        source, parse_bundle, provider = self.source_and_parse("input.md")
        assemble(self.project, source, parse_bundle, provider)
        bundle = self.project / "source-bundle"
        manifest_path = bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["imports"][0]["source_ref"] = "source-999"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(SourceBundleError, "import source_ref"):
            validate_source_bundle(bundle)

    def test_fingerprint_recomputes_from_indexed_files(self) -> None:
        source, parse_bundle, provider = self.source_and_parse("input.md")
        assemble(self.project, source, parse_bundle, provider)
        bundle = self.project / "source-bundle"
        manifest = validate_source_bundle(bundle)
        self.assertEqual(
            canonical_source_fingerprint(manifest, bundle),
            manifest["content_fingerprint"],
        )


if __name__ == "__main__":
    unittest.main()
