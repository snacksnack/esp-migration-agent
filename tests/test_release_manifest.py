"""The release manifest binds digest, SBOM and schema to one tag (RC1-640)."""

import hashlib
import json
import tomllib

import pytest

from release.manifest import PYPROJECT, ManifestError, build, main
from review_ui.db_setup import SCHEMA_COMPATIBLE_WITH, SCHEMA_VERSION

VERSION = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
COMMIT = "a" * 40
DIGEST = "sha256:" + "b" * 64
IMAGE = "ghcr.io/snacksnack/esp-migration-agent"


@pytest.fixture()
def sbom(tmp_path):
    path = tmp_path / "sbom.spdx.json"
    path.write_text('{"spdxVersion": "SPDX-2.3"}')
    return path


def test_manifest_binds_digest_sbom_hash_and_schema(sbom):
    m = build(VERSION, COMMIT, IMAGE, DIGEST, sbom)
    assert m["schema"] == "esp.release/1"
    assert m["source"] == {"repository": "snacksnack/esp-migration-agent",
                           "tag": f"v{VERSION}", "commit": COMMIT}
    assert m["image"] == {"repository": IMAGE, "digest": DIGEST, "platform": "linux/amd64"}
    assert m["sbom"]["sha256"] == hashlib.sha256(sbom.read_bytes()).hexdigest()
    assert m["db_schema"] == {"version": SCHEMA_VERSION, "compatible_with": SCHEMA_COMPATIBLE_WITH}


@pytest.mark.parametrize("field,value,match", [
    ("version", "9.9.9", "does not match pyproject"),
    ("commit", "abc123", "full 40-char sha"),
    ("digest", "latest", "sha256:<64 hex>"),
    ("image", IMAGE + ":v1", "bare repository"),
    ("image", IMAGE + "@" + DIGEST, "bare repository"),
])
def test_manifest_refuses_anything_but_an_exact_reference(sbom, field, value, match):
    args = {"version": VERSION, "commit": COMMIT, "image": IMAGE, "digest": DIGEST}
    args[field] = value
    with pytest.raises(ManifestError, match=match):
        build(sbom=sbom, **args)


def test_cli_writes_stable_json(tmp_path, sbom):
    out = tmp_path / "release.json"
    argv = ["--version", VERSION, "--commit", COMMIT, "--image", IMAGE,
            "--digest", DIGEST, "--sbom", str(sbom), "--out", str(out)]
    assert main(argv) == 0
    first = out.read_text()
    assert main(argv) == 0
    assert out.read_text() == first
    assert json.loads(first)["version"] == VERSION


def test_schema_constants_are_coherent():
    assert 1 <= SCHEMA_COMPATIBLE_WITH <= SCHEMA_VERSION
