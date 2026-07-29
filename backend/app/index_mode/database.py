# Vendor scripts used: build_db.py
import json
import subprocess
import sys
from pathlib import Path

from .. import config


def assemble_structured(
    sheets: list[dict],
    schedules: list[dict],
    instances: list[dict],
    notes: list[dict],
    relationships: list[dict],
    context_notes: list[dict],
    placeholders: list[dict],
) -> dict:
    """Step 5d: the structured.json shape build_db.py loads -- one key per table, each a
    list of flat row dicts (references/schema_guidance.md's universal tables)."""
    return {
        "sheets": sheets,
        "schedules": schedules,
        "instances": instances,
        "notes": notes,
        "relationships": relationships,
        "context_notes": context_notes,
        "placeholders": placeholders,
    }


def run_build_db(structured_path: Path, sqlite_out: Path) -> None:
    """Step 5d: load structured.json -> project.sqlite, via the vendored build_db.py."""
    cmd = [sys.executable, str(config.SCRIPTS_DIR / "build_db.py"), str(structured_path), "-o", str(sqlite_out)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"build_db.py failed:\n{result.stderr}")
