"""Validate a stable release tag against the manifest and changelog."""

import argparse
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Validate the checked-out tag and optionally extract its release notes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notes-file", type=Path)
    args = parser.parse_args()
    tag = os.environ.get("RELEASE_TAG", "")
    if os.environ.get("RELEASE_REF_TYPE") != "tag":
        raise SystemExit("Run releases on a version tag, not a branch")
    if not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag):
        raise SystemExit("Expected a stable semantic version tag such as v0.2.1")
    manifest = json.loads(
        (ROOT / "custom_components/ha_power_bill_au/manifest.json").read_text()
    )
    if tag != f"v{manifest['version']}":
        raise SystemExit("Release tag does not match manifest version")
    changelog = (ROOT / "CHANGELOG.md").read_text()
    match = re.search(
        rf"^## \[{re.escape(tag)}\] - (\d{{4}}-\d{{2}}-\d{{2}})\n(.*?)(?=^## |\Z)",
        changelog,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match or not match[2].strip():
        raise SystemExit("Changelog must contain dated, nonempty notes for this tag")
    if args.notes_file:
        args.notes_file.write_text(match[2].strip() + "\n")
    print(f"Release metadata OK: {tag}")


if __name__ == "__main__":
    main()
