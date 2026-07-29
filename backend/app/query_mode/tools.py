# Vendor scripts used: query_drawing.py (query_pdf only -- the other tools here read
# markdown files directly or hit the SQLite database, no vendor script involved)
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

from openai import OpenAI

from .. import config, db

client = OpenAI(api_key=config.OPENAI_API_KEY)


def list_sheets(doc_dir: Path) -> dict:
    """Light mode only: the sheet list Step 2 already produced (no classification has run,
    so this is just stems -- the model reads each sheet's own title block to identify it)."""
    path = doc_dir / "sheet_index.json"
    if not path.exists():
        return {"error": "no sheet index found"}
    data = json.loads(path.read_text())
    return {"sheets": [s["stem"] for s in data.get("sheets", [])]}


def query_sql(conn, sql: str) -> dict:
    """Q2: database first. Read-only SELECT against sheets/schedules/instances/notes/
    relationships/context_notes/placeholders."""
    try:
        return {"rows": db.run_readonly_sql(conn, sql)}
    except Exception as e:  # noqa: BLE001 - surfaced to the model, not the user
        return {"error": str(e)}


def read_markdown(doc_dir: Path, kind: str, name: str | None = None) -> dict:
    """Q1/prose fallback: read a markdown artefact directly, the way an interactive session
    would just open the file -- 'index' for drawings.md, 'sheet' for one sheet's markdown,
    'concept' for a concept-wiki page (or its index)."""
    if kind == "index":
        path = doc_dir / "drawings.md"
    elif kind == "sheet":
        if not name:
            return {"error": "sheet name required"}
        safe_name = name.replace("/", "_").replace(" ", "_")
        path = doc_dir / "drawings" / f"{safe_name}.md"
    elif kind == "concept":
        slug = name or "index"
        path = doc_dir / "concept_wiki" / f"{slug}.md"
    else:
        return {"error": f"unknown kind {kind!r}"}

    if not path.exists():
        return {"error": f"not found: {path.name}"}
    return {"content": path.read_text(encoding="utf-8")}


# ---------------------------------------------------------------------------
# Q3 -- on-demand PDF geometry/text query, via the vendored query_drawing.py.
# ---------------------------------------------------------------------------
def _resolve_scale(conn, sheet_files: dict, sheet_id: str) -> str | None:
    """Q6: 'Don't fabricate scale.' Look up the scale saved for this sheet at ingest time
    (from its title block); if that's missing (or there's no database at all -- light mode),
    scan the sheet's own page-info as a fallback. Never guess."""
    if conn is not None:
        row = conn.execute("SELECT scale FROM sheets WHERE sheet_id = ?", (sheet_id,)).fetchone()
        if row and row["scale"] and re.search(r"1\s*:\s*\d+", row["scale"]):
            m = re.search(r"1\s*:\s*(\d+)", row["scale"])
            return f"1:{m.group(1)}"

    pdf_path = sheet_files.get(sheet_id)
    if not pdf_path:
        return None
    cmd = [sys.executable, str(config.SCRIPTS_DIR / "query_drawing.py"), "page-info", pdf_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    for cand in data.get("scale_candidates", []):
        m = re.search(r"1\s*:\s*(\d+)", cand.get("text", ""))
        if m:
            return f"1:{m.group(1)}"
    return None


def query_pdf(conn, sheet_files: dict, sheet_id: str, subcommand: str, bbox: list[float] | None, scale: str | None) -> dict:
    pdf_path = sheet_files.get(sheet_id)
    if not pdf_path:
        return {"error": f"unknown sheet_id {sheet_id!r}"}

    scale_auto_resolved = False
    if not scale and subcommand == "polygons":
        scale = _resolve_scale(conn, sheet_files, sheet_id)
        scale_auto_resolved = scale is not None

    cmd = [sys.executable, str(config.SCRIPTS_DIR / "query_drawing.py"), subcommand, pdf_path]
    if bbox:
        cmd += ["--bbox", *[str(v) for v in bbox]]
    if scale:
        cmd += ["--scale", scale]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return {"error": result.stderr.strip()}

    data = json.loads(result.stdout)
    if subcommand == "polygons":
        data["scale_source"] = "auto-resolved" if scale_auto_resolved else ("provided" if scale else None)
        if not scale:
            data["scale_note"] = "no scale could be resolved -- state this plainly, do not guess an area"
    return data


# ---------------------------------------------------------------------------
# Q4 -- last-resort visual read of a sheet's rendered image.
# ---------------------------------------------------------------------------
def view_sheet_image(sheet_images: dict, sheet_id: str, question: str) -> dict:
    image_path = sheet_images.get(sheet_id)
    if not image_path or not Path(image_path).exists():
        return {"error": f"no rendered image available for sheet_id {sheet_id!r}"}

    b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
    response = client.chat.completions.create(
        model=config.STRONG_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are reading one construction drawing sheet image to answer one specific "
                    "question. Only state what you can actually see; if it isn't legible or isn't "
                    "shown on this sheet, say so rather than guessing."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            },
        ],
    )
    return {"visual_answer": response.choices[0].message.content or "", "sheet_id": sheet_id}
