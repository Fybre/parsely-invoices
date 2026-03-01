"""
Bootstrap script to ensure essential configuration files exist in the config volume.
Copies factory defaults from /app/defaults to /app/config if files are missing,
and repairs files that are corrupt or missing fields added in later versions.
"""
import json
import os
import shutil
from pathlib import Path

# Project structure
PROJECT_ROOT = Path(__file__).parent
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", str(PROJECT_ROOT / "config")))
DEFAULTS_DIR = PROJECT_ROOT / "defaults"


def _load_json(path: Path) -> dict | list | None:
    """Return parsed JSON from path, or None if unreadable / invalid."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _write_json(path: Path, data: dict | list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _repair_or_copy(src: Path, dst: Path, label: str) -> None:
    """Restore dst from src if dst is missing or contains invalid JSON."""
    if not dst.exists() or dst.stat().st_size == 0:
        print(f"[Bootstrap] Restoring missing file: {label}")
        shutil.copy2(src, dst)
    elif _load_json(dst) is None:
        print(f"[Bootstrap] Repairing invalid JSON in: {label}")
        shutil.copy2(src, dst)


def _merge_pipeline_settings(src: Path, dst: Path) -> None:
    """
    Ensure pipeline_settings.json contains every key present in the default.
    Keys missing from the live file (added in newer versions) are backfilled
    with the default value.  Existing values are never overwritten.
    """
    _repair_or_copy(src, dst, "pipeline_settings.json")

    default = _load_json(src) or {}
    current = _load_json(dst) or {}

    # Keys to backfill: in default but absent from current (skip _README / meta)
    missing = {
        k: v for k, v in default.items()
        if not k.startswith("_") and k not in current
    }
    if missing:
        print(f"[Bootstrap] Backfilling {len(missing)} new setting(s) into pipeline_settings.json: {list(missing)}")
        current.update(missing)
        _write_json(dst, current)


def _merge_standard_fields(src: Path, dst: Path) -> None:
    """
    Ensure standard_fields.json is valid and contains every field entry present
    in the default.  New field entries (added in newer versions) are appended
    with their default mandatory/hidden values.  Existing entries are untouched.
    """
    _repair_or_copy(src, dst, "standard_fields.json")

    default_data = _load_json(src) or {}
    current_data = _load_json(dst) or {}

    # Structural repair: must have a "fields" list
    if not isinstance(current_data.get("fields"), list):
        print("[Bootstrap] Repairing missing 'fields' key in standard_fields.json")
        shutil.copy2(src, dst)
        return

    existing_names = {entry["name"] for entry in current_data["fields"] if "name" in entry}
    new_entries = [
        entry for entry in default_data.get("fields", [])
        if entry.get("name") and entry["name"] not in existing_names
    ]
    if new_entries:
        print(f"[Bootstrap] Adding {len(new_entries)} new field(s) to standard_fields.json: {[e['name'] for e in new_entries]}")
        current_data["fields"].extend(new_entries)
        current_data["fields"].sort(key=lambda e: e.get("name", ""))
        _write_json(dst, current_data)


def ensure_config_files():
    """Verify and restore missing or corrupt config files from the defaults folder."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if not DEFAULTS_DIR.exists():
        print(f"[Bootstrap] Warning: Defaults directory not found at {DEFAULTS_DIR}")
        return

    # 1. Simple JSON files — copy if missing, repair if invalid JSON
    simple_json = ["column_keys.json", "custom_fields.json", "internal_companies.json"]
    for filename in simple_json:
        src = DEFAULTS_DIR / filename
        dst = CONFIG_DIR / filename
        if src.exists():
            _repair_or_copy(src, dst, filename)

    # 2. users.json — same repair, but always checked even when present
    users_src = DEFAULTS_DIR / "users.json"
    users_dst = CONFIG_DIR / "users.json"
    if users_src.exists():
        _repair_or_copy(users_src, users_dst, "users.json")

    # 3. pipeline_settings.json — copy if missing, repair if invalid, backfill new keys
    ps_src = DEFAULTS_DIR / "pipeline_settings.json"
    ps_dst = CONFIG_DIR / "pipeline_settings.json"
    if ps_src.exists():
        _merge_pipeline_settings(ps_src, ps_dst)

    # 4. standard_fields.json — copy if missing, repair if invalid, add new field entries
    sf_src = DEFAULTS_DIR / "standard_fields.json"
    sf_dst = CONFIG_DIR / "standard_fields.json"
    if sf_src.exists():
        _merge_standard_fields(sf_src, sf_dst)

    # 5. Jinja2 templates — copy any that are missing
    for src_template in DEFAULTS_DIR.glob("*.j2"):
        dst_template = CONFIG_DIR / src_template.name
        if not dst_template.exists():
            print(f"[Bootstrap] Restoring missing template: {src_template.name}")
            shutil.copy2(src_template, dst_template)

    # 6. Data files
    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    if not (data_dir / "projects.csv").exists() and (DEFAULTS_DIR / "projects.csv").exists():
        print("[Bootstrap] Restoring missing data file: projects.csv")
        shutil.copy2(DEFAULTS_DIR / "projects.csv", data_dir / "projects.csv")


if __name__ == "__main__":
    ensure_config_files()
