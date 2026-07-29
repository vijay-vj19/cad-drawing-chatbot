# Vendor scripts used: extract_instances.py
import json
import subprocess
import sys
from pathlib import Path

from .. import config

# SKILL.md Step 5b's own suggested default: sheets that show a demolition/existing view
# alongside a renovation view tag the same physical unit twice unless filtered out.
DEMO_EXISTING_PATTERN = "DEMOLITION|EXISTING"

# Never run instance extraction on these types -- a tag inside a schedule/notes sheet is a
# DEFINITION, not a placed instance (SKILL.md Step 5b: "running there returns phantom counts").
PLAN_TYPES = {"general_arrangement"}


def run_extract_instances(
    extraction_path: str, pattern: str, sheet_label: str, exclude_bboxes: list, out_path: Path, exclude_pattern: str | None = None
) -> list[dict]:
    """Step 5b: tag -> (x, y) instance, via the vendored extract_instances.py. Pure regex
    match against real text positions -- no LLM, deterministic."""
    args = [
        sys.executable,
        str(config.SCRIPTS_DIR / "extract_instances.py"),
        str(extraction_path),
        "--pattern",
        pattern,
        "--sheet",
        sheet_label,
        "--space-tolerant",
        "--exclude-pattern",
        exclude_pattern or DEMO_EXISTING_PATTERN,
        "-o",
        str(out_path),
    ]
    # exclude_bboxes should be a list of [x0,y0,x1,y1] boxes, but the LLM occasionally
    # flattens a single box to [x0,y0,x1,y1] directly when there's only one -- normalise
    # that shape rather than crash the whole upload over one malformed field.
    boxes = exclude_bboxes or []
    if boxes and all(isinstance(v, (int, float)) for v in boxes):
        boxes = [boxes]
    for box in boxes:
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        args += ["--exclude", ",".join(str(v) for v in box)]

    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"extract_instances.py failed:\n{result.stderr}")
    return json.loads(out_path.read_text())


def extract_all_instances(sheet: dict, drawing_type: str, instance_targets: list[dict], out_dir: Path) -> list[dict]:
    """Runs Step 5b for every instance_target proposed for one sheet (from analysis.py's
    Step 5 output) and returns them as rows ready for the `instances` table."""
    if drawing_type not in PLAN_TYPES:
        return []

    rows = []
    for i, target in enumerate(instance_targets):
        pattern = target.get("pattern")
        if not pattern:
            continue
        out_path = out_dir / f"instances_{sheet['stem']}_{i}.json"
        try:
            extracted = run_extract_instances(
                sheet["extraction_path"],
                pattern,
                sheet.get("sheet_id") or sheet["stem"],
                target.get("exclude_bboxes", []),
                out_path,
            )
        except RuntimeError:
            continue
        for r in extracted:
            rows.append(
                {
                    "tag": r["tag"],
                    "entity_type": target.get("entity_type"),
                    "x": r["x"],
                    "y": r["y"],
                    "sheet_id": sheet.get("sheet_id") or sheet["stem"],
                    "grid": None,
                    "properties": None,
                    "reliability": "HIGH",
                }
            )

    # Two proposed patterns on the same sheet can overlap and match the same physical tag
    # twice (identical tag+coordinates) -- that's one instance, not two, so dedupe exact
    # (tag, x, y, sheet_id) matches before they reach the database.
    seen = set()
    deduped = []
    for row in rows:
        key = (row["tag"], row["x"], row["y"], row["sheet_id"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped
