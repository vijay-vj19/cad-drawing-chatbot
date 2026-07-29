# Vendor scripts used: crop_region.py
import json
import re
import subprocess
import sys
from pathlib import Path

from openai import OpenAI

from .. import config

client = OpenAI(api_key=config.OPENAI_API_KEY)

# Legend template fields, from references/drawing_types.md's `legend` per-type template.
LEGEND_PROMPT = """You are reading a LEGEND sheet from a construction drawing set -- it defines the symbols
used elsewhere in this discipline's drawings.

You are given this sheet's extracted text blocks, each with its exact bounding box in PDF
points. Extract EVERY symbol definition you can find: its ID/shorthand as printed (or an ID
you infer if none is given), its description verbatim, and the bounding box of that symbol's
entry in the legend (so it can be cropped from the sheet later). Also capture any
abbreviations list on this sheet.

Never invent a symbol that isn't actually shown. Output strictly valid JSON, no commentary,
matching this schema:
{"symbols": [{"id": "...", "description": "...", "bbox": [x0, y0, x1, y1]}],
 "abbreviations": [{"abbr": "...", "meaning": "..."}]}"""


def extract_legend_symbols(sheet: dict) -> dict:
    """Step 4: one LLM call per legend sheet (cheap model, text-only -- legend routes to
    'text' in the routing table since the symbol IDs/descriptions are in the vector text)."""
    extraction = json.loads(Path(sheet["extraction_path"]).read_text())
    payload = {"text_blocks": extraction.get("text_blocks", [])[:400]}
    response = client.chat.completions.create(
        model=config.CHEAP_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": LEGEND_PROMPT},
            {"role": "user", "content": json.dumps(payload)},
        ],
    )
    return json.loads(response.choices[0].message.content)


def crop_symbol(pdf_path: str, bbox: list[float], output_path: Path) -> None:
    """Cut the symbol's bbox out of its legend sheet PDF into a small PNG -- pure geometry,
    no LLM, via the vendored crop_region.py."""
    cmd = [
        sys.executable,
        str(config.SCRIPTS_DIR / "crop_region.py"),
        pdf_path,
        "--bbox",
        *[str(v) for v in bbox],
        "-o",
        str(output_path),
        "--long-edge-px",
        "512",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"crop_region.py failed:\n{result.stderr}")


def _safe_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_") or "symbol"


def build_symbol_library(legend_sheets: list[dict], crops_dir: Path) -> dict:
    """Runs Step 4 across every legend-classified sheet and aggregates the result into the
    symbol_library.json shape from references/output_schemas.md."""
    crops_dir.mkdir(parents=True, exist_ok=True)
    library_sheets = []
    symbols = []
    used_names: set[str] = set()

    for sheet in legend_sheets:
        result = extract_legend_symbols(sheet)
        library_sheets.append(
            {"stem": sheet["stem"], "sheet_id": sheet.get("sheet_id"), "discipline": sheet.get("discipline")}
        )
        for sym in result.get("symbols", []):
            bbox = sym.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            base_name = _safe_filename(f"{sheet.get('discipline', '')}_{sym.get('id', '')}")
            name = base_name
            i = 2
            while name in used_names:
                name = f"{base_name}_{i}"
                i += 1
            used_names.add(name)

            crop_path = crops_dir / f"{name}.png"
            try:
                crop_symbol(sheet["single_sheet_pdf"], bbox, crop_path)
            except RuntimeError:
                continue

            symbols.append(
                {
                    "id": sym.get("id"),
                    "description": sym.get("description"),
                    "discipline": sheet.get("discipline"),
                    "image_path": str(crop_path),
                    "source_legend_stem": sheet["stem"],
                    "source_legend_sheet_id": sheet.get("sheet_id"),
                    "source_bbox": bbox,
                }
            )

    return {"legend_sheets": library_sheets, "symbols": symbols}
