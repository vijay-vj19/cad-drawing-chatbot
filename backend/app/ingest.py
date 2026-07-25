import json
import subprocess
import sys
from pathlib import Path

import fitz  # PyMuPDF
from openai import OpenAI

from . import config, db

client = OpenAI(api_key=config.OPENAI_API_KEY)


class GateRejected(Exception):
    """Raised when the uploaded PDF has no usable vector text layer."""


# ---------------------------------------------------------------------------
# Step 1 — gate on text layer
# ---------------------------------------------------------------------------
def gate_check(pdf_path: Path) -> None:
    doc = fitz.open(pdf_path)
    try:
        if doc.page_count == 0:
            raise GateRejected("PDF has no pages.")
        counts = [len(page.get_text()) for page in doc]
    finally:
        doc.close()
    avg_chars = sum(counts) / len(counts)
    if avg_chars < config.TEXT_LAYER_MIN_CHARS_PER_PAGE:
        raise GateRejected("no text layer, upload a CAD-plotted PDF")


# ---------------------------------------------------------------------------
# Skill script wrappers (all deterministic, no LLM)
# ---------------------------------------------------------------------------
def _run_script(args: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"Script failed: {args[0]}\n{result.stderr}")


def run_process_drawing(pdf_path: Path, split_dir: Path) -> dict:
    _run_script([str(config.SCRIPTS_DIR / "process_drawing.py"), str(pdf_path), "-o", str(split_dir)])
    return json.loads((split_dir / "manifest.json").read_text())


def run_extract_instances(
    sheet_json: Path, pattern: str, sheet_label: str, exclude_bboxes: list, out_path: Path
) -> list[dict]:
    args = [
        str(config.SCRIPTS_DIR / "extract_instances.py"),
        str(sheet_json),
        "--pattern", pattern,
        "--sheet", sheet_label,
        "--space-tolerant",
        "-o", str(out_path),
    ]
    for box in exclude_bboxes or []:
        args += ["--exclude", ",".join(str(v) for v in box)]
    _run_script(args)
    return json.loads(out_path.read_text())


def run_build_db(structured_path: Path, sqlite_out: Path) -> None:
    _run_script([str(config.SCRIPTS_DIR / "build_db.py"), str(structured_path), "-o", str(sqlite_out)])


# ---------------------------------------------------------------------------
# Step 3 — one GPT-4o call: classify sheets, parse schedules/notes, propose
# instance tag patterns + exclusion boxes, write per-sheet markdown.
# ---------------------------------------------------------------------------
EXTRACTION_SYSTEM_PROMPT = """You are extracting structured data from a construction drawing set for a database. \
You will be given, for every sheet, its raw vector-extracted text blocks (each with exact bbox coordinates in PDF points) \
and any title-block candidates already detected positionally.

Return a single JSON object with exactly these keys:
- "sheets": one entry per sheet: {"sheet_index", "number", "title", "discipline", "scale"} \
  (number/title/scale read from the title block; discipline inferred, e.g. "structural", "architectural").
- "schedules": one row per schedule entry found (e.g. a footing schedule, door schedule, window schedule, \
  room/space/finish schedule). Parse schedule tables COMPLETELY, one row per type/mark: \
  {"sheet_index", "type", "mark", "properties", "reliability": "HIGH"}. \
  "properties" is a short human-readable string of the row's other columns (size, rating, material, AREA, etc). \
  If a room/space schedule lists an area (SF, SQ FT, m2), put it in "properties" verbatim (e.g. "AREA: 850 SF") \
  -- this is a common, exact, HIGH-reliability way area questions get answered without any geometry math.
- "notes": general/sheet notes blocks: {"sheet_index", "category", "text", "reliability": "HIGH"}. Also include, \
  as its own row here, any room/space area printed directly on a PLAN next to a room name (architectural plans \
  very often print e.g. "LOBBY 850 SF" right under/beside the room label) -- {"category": "room_area", \
  "text": "LOBBY: 850 SF"}. This is the single most reliable source for an area question and should be captured \
  whenever visible, so a later question doesn't need to fall back to fragile on-demand polygon geometry.
- "instance_targets": one entry per COUNTABLE, REPEATED thing that appears as text directly placed on a plan \
  view (not description prose) -- so a later question like "how many X" or "where are the X" can be answered \
  exactly. Two cases, both go in this same list:
  (a) A schedule's marks placed as tags on a plan (e.g. F10/F12/F14 footing tags, D01-D06 door tags) -- the \
      classic case, `pattern` matches the mark exactly as it appears on the plan.
  (b) NO schedule at all, but a repeated room/space TYPE label is written directly on a plan (very common on \
      residential/small sets -- e.g. "BEDROOM 1", "BEDROOM 2", "BATHROOM", "BATH 2") -- propose a pattern that \
      matches the type, e.g. "BEDROOM\\\\s*\\\\d*" or "BATHROOM\\\\s*\\\\d*", so instances of that room type get \
      counted and located even without any schedule backing them. Do this for any plan sheet with 2+ instances \
      of the same room/space type.
  In both cases: {"sheet_index" (the sheet showing the PLAN with the tags/labels), "pattern" (a Python regex), \
  "exclude_bboxes" (list of [x0,y0,x1,y1] regions on that sheet to exclude — a schedule table, legend, or title \
  block that would otherwise double-count a definition as a placed instance; for case (b) with no schedule this \
  is usually just the title block, or can be empty)}. Never skip proposing an instance_target just because \
  there's no formal schedule -- repeated labels directly on a plan are instances too.
- "markdown": one entry per sheet: {"sheet_index", "content"} — a concise markdown summary of that sheet: \
  title, discipline, scale, a prose description of what's on it, and its schedules/notes rendered as tables. \
  This is for retrieval, not for counting. On a PLAN sheet, if any room/space name labels are visible (e.g. \
  "LOBBY", "MECHANICAL 105", "OFFICE"), end the markdown with a "## Coordinate hints" section listing each one \
  with its approximate bbox from the sheet's text blocks, e.g. "- LOBBY: near [120, 340, 180, 355]", and its \
  printed area if one is shown nearby, e.g. "- LOBBY: near [120, 340, 180, 355], AREA: 850 SF". This is what \
  lets a later question like "square footage of the lobby" be answered by locating the name first (or reading \
  the area straight off, if already printed), rather than scanning the whole sheet blind. Omit the section if \
  the sheet has no such labels.

Never invent a value. Store counts/marks as they literally appear. If a sheet has no schedules or no taggable \
plan, omit it from the relevant list. Output strictly valid JSON, no commentary."""


def call_extraction_model(sheets_payload: list[dict]) -> dict:
    response = client.chat.completions.create(
        model=config.CHAT_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({"sheets": sheets_payload})},
        ],
    )
    return json.loads(response.choices[0].message.content)


def embed_text(text: str) -> list[float]:
    response = client.embeddings.create(model=config.EMBEDDING_MODEL, input=text)
    return response.data[0].embedding


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _build_sheets_payload(manifest: dict) -> tuple[list[dict], dict]:
    """Load each sheet's vector-extraction JSON and build the prompt payload
    plus a sheet_index -> file-location map for later steps."""
    payload = []
    file_map = {}
    for i, sheet in enumerate(manifest["sheets"], start=1):
        extraction_path = Path(sheet["extraction_path"])
        data = json.loads(extraction_path.read_text())
        stem = Path(sheet["pdf_path"]).stem
        file_map[i] = {
            "stem": stem,
            "pdf_path": sheet["pdf_path"],
            "extraction_path": str(extraction_path),
        }
        payload.append(
            {
                "sheet_index": i,
                "stem": stem,
                "title_block_candidates": [
                    {"text": b["text"], "bbox": [round(v, 1) for v in b["bbox"]]}
                    for b in data.get("title_block_candidates", [])
                ],
                "scale_detected": data.get("scale", {}),
                "text_blocks": [
                    {"text": b["text"], "bbox": [round(v, 1) for v in b["bbox"]]}
                    for b in data.get("text_blocks", [])[:800]
                ],
            }
        )
    return payload, file_map


def ingest_pdf(doc_id: str, pdf_path: Path) -> dict:
    gate_check(pdf_path)

    d = config.doc_dir(doc_id)
    split_dir = d / "drawings_split"
    manifest = run_process_drawing(pdf_path, split_dir)

    sheets_payload, file_map = _build_sheets_payload(manifest)
    extraction = call_extraction_model(sheets_payload)

    sheet_info = {s["sheet_index"]: s for s in extraction.get("sheets", [])}

    def number_for(idx: int) -> str:
        info = sheet_info.get(idx, {})
        return info.get("number") or file_map.get(idx, {}).get("stem", f"sheet{idx}")

    sheets_rows = [
        {
            "number": number_for(info["sheet_index"]),
            "title": info.get("title"),
            "discipline": info.get("discipline"),
            "scale": info.get("scale"),
            "source_sheet": number_for(info["sheet_index"]),
        }
        for info in extraction.get("sheets", [])
    ]

    # Persist number -> single-sheet PDF path so /chat's query_pdf tool can find the
    # right file without re-deriving it from the model's output.
    sheet_files = {number_for(idx): info["pdf_path"] for idx, info in file_map.items()}
    (d / "sheet_files.json").write_text(json.dumps(sheet_files, indent=2))

    schedules_rows = [
        {
            "type": row.get("type"),
            "mark": row.get("mark"),
            "properties": row.get("properties"),
            "source_sheet": number_for(row.get("sheet_index")),
            "reliability": row.get("reliability", "HIGH"),
        }
        for row in extraction.get("schedules", [])
    ]

    notes_rows = [
        {
            "category": row.get("category"),
            "text": row.get("text"),
            "source_sheet": number_for(row.get("sheet_index")),
            "reliability": row.get("reliability", "HIGH"),
        }
        for row in extraction.get("notes", [])
    ]

    instances_rows = []
    for target_idx, target in enumerate(extraction.get("instance_targets", [])):
        idx = target.get("sheet_index")
        file_info = file_map.get(idx)
        pattern = target.get("pattern")
        if not file_info or not pattern:
            continue
        out_path = split_dir / f"instances_{idx}_{target_idx}.json"
        try:
            results = run_extract_instances(
                Path(file_info["extraction_path"]),
                pattern,
                number_for(idx),
                target.get("exclude_bboxes", []),
                out_path,
            )
        except (RuntimeError, json.JSONDecodeError):
            continue
        for r in results:
            instances_rows.append(
                {
                    "tag": r["tag"],
                    "x": r["x"],
                    "y": r["y"],
                    "source_sheet": number_for(idx),
                    "reliability": "HIGH",
                }
            )

    structured = {
        "sheets": sheets_rows,
        "schedules": schedules_rows,
        "instances": instances_rows,
        "notes": notes_rows,
    }
    structured_path = d / "structured.json"
    structured_path.write_text(json.dumps(structured, indent=2))
    run_build_db(structured_path, config.sqlite_path(doc_id))

    # Per-sheet markdown -> embed -> store in the `chunks` table (simple in-SQLite vector store).
    conn = db.get_conn(doc_id)
    # build_db.py skips creating a table when its list is empty (e.g. no schedules on this
    # set) -- make sure sheets/schedules/instances/notes always exist so query_sql gets an
    # empty result instead of a "no such table" error.
    db.ensure_core_tables(conn)
    db.ensure_chunks_table(conn)
    markdown_dir = d / "markdown"
    markdown_dir.mkdir(exist_ok=True)
    for md in extraction.get("markdown", []):
        idx = md.get("sheet_index")
        content = md.get("content")
        if not content:
            continue
        number = number_for(idx)
        safe_name = number.replace("/", "_").replace(" ", "_")
        (markdown_dir / f"{safe_name}.md").write_text(content, encoding="utf-8")
        embedding = embed_text(content)
        db.insert_chunk(conn, number, "HIGH", content, embedding)

    counts = db.table_counts(conn)
    conn.close()

    return {
        "doc_id": doc_id,
        "sheet_count": len(sheets_rows),
        "sheets": [
            {"number": s["number"], "title": s["title"], "discipline": s["discipline"], "scale": s["scale"]}
            for s in sheets_rows
        ],
        "schedule_count": counts.get("schedules", 0),
        "instance_count": counts.get("instances", 0),
        "note_count": counts.get("notes", 0),
    }
