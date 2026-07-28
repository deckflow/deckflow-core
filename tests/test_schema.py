"""The published schemas must describe what the code actually emits.

Core has no third-party dependencies, so rather than pull in `jsonschema` this
implements the small subset of JSON Schema the two published files use. That is
enough to catch the failure that matters: the schema and the implementation
drifting apart between releases.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from deckflow_core import schemas_dir
from deckflow_core.diagnostics import Diagnostic
from deckflow_core.envelope import Envelope
from deckflow_core.providers import matrix
from deckflow_core.providers.resolve import Resolution

_TYPES = {
    "object": dict, "array": list, "string": str,
    "integer": int, "boolean": bool, "number": (int, float),
}


def validate(instance, schema: dict, root: dict, path: str = "$") -> list[str]:
    """Return a list of human-readable violations; empty means valid."""
    errors: list[str] = []

    if "$ref" in schema:
        pointer = schema["$ref"].lstrip("#/").split("/")
        target = root
        for part in pointer:
            target = target[part]
        return validate(instance, target, root, path)

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {instance!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in {schema['enum']}")

    declared = schema.get("type")
    if declared:
        allowed = declared if isinstance(declared, list) else [declared]
        if "null" in allowed and instance is None:
            return errors
        expected = tuple(_TYPES[name] for name in allowed if name in _TYPES)
        if expected and not isinstance(instance, expected):
            errors.append(f"{path}: expected {declared}, got {type(instance).__name__}")
            return errors

    if "pattern" in schema and isinstance(instance, str):
        if not re.search(schema["pattern"], instance):
            errors.append(f"{path}: {instance!r} does not match {schema['pattern']}")

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required key {key!r}")
        properties = schema.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                errors.extend(validate(value, properties[key], root, f"{path}.{key}"))
            elif "additionalProperties" in schema and isinstance(schema["additionalProperties"], dict):
                errors.extend(validate(value, schema["additionalProperties"], root, f"{path}.{key}"))

    if isinstance(instance, list) and "items" in schema:
        for index, item in enumerate(instance):
            errors.extend(validate(item, schema["items"], root, f"{path}[{index}]"))

    return errors


def load(name: str) -> dict:
    return json.loads((schemas_dir() / name).read_text(encoding="utf-8"))


class SchemasAreReachableTest(unittest.TestCase):
    def test_both_schemas_ship(self):
        self.assertTrue((schemas_dir() / "envelope.schema.json").is_file())
        self.assertTrue((schemas_dir() / "providers.schema.json").is_file())


class EnvelopeConformsTest(unittest.TestCase):
    def setUp(self):
        self.schema = load("envelope.schema.json")

    def _check(self, envelope: Envelope):
        errors = validate(envelope.to_json(), self.schema, self.schema)
        self.assertEqual(errors, [], "\n".join(errors))

    def test_minimal_envelope(self):
        self._check(Envelope(command="providers"))

    def test_envelope_with_every_optional_part(self):
        envelope = Envelope(command="providers.install")
        envelope.status = "partial"
        envelope.providers = [
            Resolution(spec=matrix.get("deckhtml"), resolution="acquired",
                       status="ready", version="0.3.2", acquired=True,
                       path="/tmp/x/node_modules/.bin/deckhtml").to_json()
        ]
        envelope.inputs = [{"path": "/abs/in.pdf", "sha256": "a" * 64, "bytes": 12}]
        envelope.outputs = [{"path": "/abs/out.pptx"}]
        envelope.provider_result = {"ok": True, "mode": "local"}
        envelope.add(
            Diagnostic(rule_id="PROVIDER_ACQUIRED", severity="info", message="m",
                       location="/tmp/x", expected="e", actual="a", recovery="r")
        )
        self._check(envelope)

    def test_blocked_provider_conforms(self):
        envelope = Envelope(command="providers")
        envelope.providers = [
            Resolution(spec=matrix.get("editor"), resolution="missing",
                       status="blocked", blocked_by="node-runtime-missing").to_json()
        ]
        self._check(envelope)

    def test_schema_rejects_a_status_the_code_cannot_emit(self):
        payload = Envelope(command="providers").to_json()
        payload["status"] = "mostly-fine"
        self.assertTrue(validate(payload, self.schema, self.schema))

    def test_schema_rejects_a_lowercase_rule_id(self):
        payload = Envelope(command="providers").to_json()
        payload["diagnostics"] = [{"rule_id": "lower_case", "severity": "info", "message": "m"}]
        self.assertTrue(validate(payload, self.schema, self.schema))


class PinMatrixConformsTest(unittest.TestCase):
    def test_shipped_matrix_matches_its_schema(self):
        schema = load("providers.schema.json")
        raw = json.loads(
            (Path(matrix.__file__).with_name("providers.json")).read_text(encoding="utf-8")
        )
        errors = validate(raw, schema, schema)
        self.assertEqual(errors, [], "\n".join(errors))

    def test_matrix_core_version_tracks_the_package(self):
        from deckflow_core import __version__

        raw = json.loads(
            (Path(matrix.__file__).with_name("providers.json")).read_text(encoding="utf-8")
        )
        self.assertEqual(raw["core_version"], __version__)


if __name__ == "__main__":
    unittest.main()
