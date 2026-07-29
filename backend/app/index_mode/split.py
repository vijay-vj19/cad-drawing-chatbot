# Vendor scripts used: process_drawing.py, build_sheet_index.py
import json
import subprocess
import sys
from pathlib import Path

from .. import config


def run_process_drawing(pdf_path: Path, split_dir: Path) -> dict:
    """Step 2: split the PDF into per-sheet PDFs, PNGs, and vector-extraction JSON.
    Pure script, no LLM -- process_drawing.py does the mechanical splitting."""
    cmd = [
        sys.executable,
        str(config.SCRIPTS_DIR / "process_drawing.py"),
        str(pdf_path),
        "-o",
        str(split_dir),
        "--long-edge-px",
        str(config.RENDER_LONG_EDGE_PX),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"process_drawing.py failed:\n{result.stderr}")
    return json.loads((split_dir / "manifest.json").read_text())


def build_sheet_index(drawings_split_root: Path, output_path: Path) -> dict:
    """Step 2: aggregate every sheet under drawings_split/ into one flat list --
    the deterministic 'what sheets exist' file that Step 3 classification reads."""
    cmd = [
        sys.executable,
        str(config.SCRIPTS_DIR / "build_sheet_index.py"),
        str(drawings_split_root),
        "-o",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"build_sheet_index.py failed:\n{result.stderr}")
    return json.loads(output_path.read_text())


def sheet_char_count(extraction_path: str) -> int:
    """Characters pdfplumber found on this one sheet -- the basis for the vector/raster
    fork (SKILL.md learning #5)."""
    data = json.loads(Path(extraction_path).read_text())
    return data.get("pdfplumber", {}).get("chars_count", 0)


def is_raster_sheet(extraction_path: str) -> bool:
    """True if this sheet has no usable text layer (scanned or outlined-text) -- flag it
    for vision-only routing and instance-extraction placeholders, never fabricate."""
    return sheet_char_count(extraction_path) < config.TEXT_LAYER_MIN_CHARS_PER_PAGE
