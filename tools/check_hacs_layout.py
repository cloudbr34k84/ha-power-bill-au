"""Check the repository's HACS integration packaging without HA dependencies."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Fail with a useful message when the installable package is incomplete."""
    hacs = json.loads((ROOT / "hacs.json").read_text())
    if hacs.get("content_in_root") is not False:
        raise SystemExit("hacs.json must set content_in_root to false")
    if not hacs.get("name"):
        raise SystemExit("hacs.json must define a display name")

    components = ROOT / "custom_components"
    directories = sorted(
        path.name for path in components.iterdir()
        if path.is_dir() and path.name != "__pycache__"
    )
    if directories != ["ha_power_bill_au"]:
        raise SystemExit(f"Expected only custom_components/ha_power_bill_au; found {directories}")

    integration = components / "ha_power_bill_au"
    manifest = json.loads((integration / "manifest.json").read_text())
    required = ("domain", "name", "version", "documentation", "issue_tracker", "codeowners")
    missing = [key for key in required if not manifest.get(key)]
    if missing:
        raise SystemExit(f"Missing manifest metadata: {', '.join(missing)}")
    if manifest["domain"] != integration.name:
        raise SystemExit("Manifest domain must match the integration folder name")

    for relative in ("__init__.py", "brand/icon.png", "strings.json", "translations/en.json"):
        if not (integration / relative).is_file():
            raise SystemExit(f"Missing runtime file: custom_components/ha_power_bill_au/{relative}")
    for path in integration.rglob("*.json"):
        json.loads(path.read_text())
    print("HACS layout OK: custom_components/ha_power_bill_au, root metadata and runtime JSON")


if __name__ == "__main__":
    main()
