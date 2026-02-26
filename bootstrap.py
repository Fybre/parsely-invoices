"""
Bootstrap script to ensure essential configuration files exist in the config volume.
Copies factory defaults from /app/defaults to /app/config if files are missing.
"""
import json
import os
import shutil
from pathlib import Path

# Project structure
PROJECT_ROOT = Path(__file__).parent
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", str(PROJECT_ROOT / "config")))
DEFAULTS_DIR = PROJECT_ROOT / "defaults"

def ensure_config_files():
    """Verify and restore missing config files from defaults folder."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    if not DEFAULTS_DIR.exists():
        print(f"[Bootstrap] Warning: Defaults directory not found at {DEFAULTS_DIR}")
        return

    # 1. Essential JSON data files
    json_files = ["column_keys.json", "custom_fields.json", "users.json"]
    
    for filename in json_files:
        src = DEFAULTS_DIR / filename
        dst = CONFIG_DIR / filename
        
        if not dst.exists() and src.exists():
            print(f"[Bootstrap] Restoring missing config file: {filename}")
            shutil.copy2(src, dst)
        elif dst.exists() and filename == "users.json":
            # Repair corrupted users.json
            try:
                if dst.stat().st_size == 0:
                    raise ValueError("Empty file")
                with open(dst, "r", encoding="utf-8") as f:
                    json.load(f)
            except (json.JSONDecodeError, ValueError):
                print(f"[Bootstrap] Repairing invalid users.json")
                shutil.copy2(src, dst)

    # 2. Jinja2 templates
    for src_template in DEFAULTS_DIR.glob("*.j2"):
        dst_template = CONFIG_DIR / src_template.name
        if not dst_template.exists():
            print(f"[Bootstrap] Restoring missing template: {src_template.name}")
            shutil.copy2(src_template, dst_template)

if __name__ == "__main__":
    ensure_config_files()
