"""Write the signed release manifest (RC1-640, ADR-0027).

    uv run --group ui python -m release.manifest --version 0.1.1 --commit <sha> \\
        --image ghcr.io/snacksnack/esp-migration-agent --digest sha256:... \\
        --sbom sbom.spdx.json --out release.json

The manifest is the one file the release workflow signs as a blob. It binds
the image digest, the SBOM's sha256 and the database schema compatibility
to one tag, so an installer that verifies this signature and then deploys
by digest runs exactly what CI built. Nothing here is secret.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path

from review_ui.db_setup import SCHEMA_COMPATIBLE_WITH, SCHEMA_VERSION

SCHEMA = "esp.release/1"
REPOSITORY = "snacksnack/esp-migration-agent"
PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")


class ManifestError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(version: str, commit: str, image: str, digest: str, sbom: Path) -> dict:
    project = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    if version != project:
        raise ManifestError(f"version {version} does not match pyproject ({project})")
    if not COMMIT.match(commit):
        raise ManifestError(f"commit must be a full 40-char sha, got {commit!r}")
    if not DIGEST.match(digest):
        raise ManifestError(f"digest must be sha256:<64 hex>, got {digest!r}")
    if "@" in image or ":" in image.rpartition("/")[2]:
        raise ManifestError(f"image must be a bare repository, got {image!r}")
    if not sbom.is_file():
        raise ManifestError(f"no SBOM at {sbom}")
    return {
        "schema": SCHEMA,
        "version": version,
        "source": {"repository": REPOSITORY, "tag": f"v{version}", "commit": commit},
        "image": {"repository": image, "digest": digest, "platform": "linux/amd64"},
        "sbom": {"file": sbom.name, "format": "spdx-json", "sha256": sha256_file(sbom)},
        "db_schema": {"version": SCHEMA_VERSION, "compatible_with": SCHEMA_COMPATIBLE_WITH},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--sbom", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = build(args.version, args.commit, args.image, args.digest, args.sbom)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.out}: v{manifest['version']} {manifest['image']['digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
