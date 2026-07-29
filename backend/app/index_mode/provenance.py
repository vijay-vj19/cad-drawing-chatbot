# Vendor scripts used: validate_provenance.py
import json
import subprocess
import sys
from pathlib import Path

from .. import config

# {table: [id_field, value_field, source_field]} -- matches the row field names in
# structured.json (see database.py's assemble_structured), per validate_provenance.py's
# --fields contract.
FIELDS = {
    "schedules": ["mark", "properties", "source_sheet"],
    "notes": ["topic", "text", "source_sheet"],
}


def write_sheet_text_files(text_dir: Path, sheet_texts: dict[str, str]) -> None:
    """One .txt per sheet (stem must match the source_sheet value used in structured.json,
    case-insensitively) -- what validate_provenance.py checks cited values against."""
    text_dir.mkdir(parents=True, exist_ok=True)
    for sheet_id, text in sheet_texts.items():
        safe_name = sheet_id.replace("/", "_").replace(" ", "_")
        (text_dir / f"{safe_name}.txt").write_text(text, encoding="utf-8")


def run_provenance_validation(structured_path: Path, text_dir: Path, report_path: Path) -> int:
    """Step 5e (mandatory): validate every schedules/notes row's cited source_sheet against
    that sheet's actual text; relocate mis-sourced rows. Returns the relocation count."""
    fields_path = structured_path.parent / "provenance_fields.json"
    fields_path.write_text(json.dumps(FIELDS))

    cmd = [
        sys.executable,
        str(config.SCRIPTS_DIR / "validate_provenance.py"),
        str(structured_path),
        "--textdir",
        str(text_dir),
        "--fields",
        str(fields_path),
        "--apply",
        "-o",
        str(report_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"validate_provenance.py failed:\n{result.stderr}")

    report = json.loads(report_path.read_text()) if report_path.exists() else []
    return sum(1 for row in report if str(row.get("action", "")).startswith("RELOCATE"))


def validated_structured_path(structured_path: Path) -> Path:
    return Path(str(structured_path).replace(".json", "_validated.json"))
