import base64
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
# Step 3a — classify each sheet (cheap, one combined call, text only). This is
# the routing key for step 3b: text-dominant sheets stay text-only; anything
# with a plan/section/elevation/detail (or unknown) gets its rendered PNG too.
# ---------------------------------------------------------------------------
CLASSIFICATION_SYSTEM_PROMPT = """Classify each sheet of a CAD/construction drawing set into exactly one type: \
"general_arrangement" (a plan/layout view), "section_view", "elevation", "detail", "schedule_sheet" (dominated \
by a tabular schedule), "general_notes" (dominated by notes text), "legend", "single_line_diagram", \
"cover_sheet", "coordination_drawing", or "other".

You're given each sheet's title-block text and a text sample. Judge from the title and content -- a title \
containing "PLAN" is usually general_arrangement, "ELEVATION" -> elevation, "SECTION" -> section_view, \
"SCHEDULE" -> schedule_sheet, "NOTES" -> general_notes, "DETAIL" -> detail, "LEGEND"/"SYMBOLS" -> legend, \
"COVER"/"TITLE SHEET" -> cover_sheet, "SINGLE LINE"/"RISER" -> single_line_diagram. Read the content when the \
title is ambiguous or missing. This works for any drawing discipline -- architectural, structural, civil, \
electrical, mechanical, plumbing.

Return a single JSON object: {"classifications": [{"sheet_index": <int>, "type": "<one of the types above>", \
"discipline": "<inferred>"}]}, one entry per sheet given. Output strictly valid JSON, no commentary."""

# Mirrors the vendored skill's own routing table (SKILL.md, "Routing — model + render per
# sheet"): text-dominant sheets are already 100% accurate from the vector layer and don't
# need vision; plan/section/elevation/detail sheets carry graphical content (dimension
# strings, symbols, spatial layout) that only exists as drawing, not text. Unknown defaults
# to vision, matching the skill's raster-fallback principle (never assume text is enough).
ROUTING = {
    "general_notes": "text",
    "schedule_sheet": "text",
    "legend": "text",
    "cover_sheet": "text",
    "general_arrangement": "vision",
    "section_view": "vision",
    "elevation": "vision",
    "detail": "vision",
    "single_line_diagram": "vision",
    "coordination_drawing": "vision",
    "other": "vision",
}


def route_for(sheet_type: str) -> str:
    return ROUTING.get(sheet_type, "vision")


def classify_sheets(sheets_payload: list[dict]) -> dict[int, str]:
    slim_payload = [
        {
            "sheet_index": s["sheet_index"],
            "title_block_candidates": s["title_block_candidates"],
            "text_sample": " ".join(b["text"] for b in s["text_blocks"][:60]),
        }
        for s in sheets_payload
    ]
    response = client.chat.completions.create(
        model=config.CLASSIFY_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": CLASSIFICATION_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({"sheets": slim_payload})},
        ],
    )
    data = json.loads(response.choices[0].message.content)
    return {row["sheet_index"]: row.get("type", "other") for row in data.get("classifications", [])}


# ---------------------------------------------------------------------------
# Step 3b — per-sheet extraction, routed to text-only (cheap model) or
# vision (the sheet's own rendered PNG + the vector text, strong model).
# ---------------------------------------------------------------------------
EXTRACTION_SYSTEM_PROMPT = """You are extracting structured data from ONE sheet of a CAD/construction drawing \
set for a database. This could be any type of drawing set -- a house, an apartment building, a shop fit-out, a \
multi-storey/skyscraper tower, a road/civil plan, an electrical/mechanical/plumbing layout, or anything else. \
Do not assume a residential or commercial building; read what's actually on the sheet and adapt.

You are given this sheet's raw vector-extracted text blocks (each with exact bbox coordinates in PDF points) \
and any title-block candidates already detected positionally.

The goal is to capture EVERY distinct, nameable thing on this sheet as structured, queryable data -- rooms/ \
spaces, doors, windows, staircases, structural elements (footings, columns, beams, slabs), fixtures/equipment, \
civil elements (manholes, catch basins, curbs, light poles, road segments), electrical/mechanical components, \
or anything else that appears as a distinct labeled/tagged item. Nothing discipline-specific is hardcoded here \
-- infer the vocabulary from what the drawing actually shows.

Return a single JSON object with exactly these keys:

- "sheet": {"number", "title", "discipline", "scale"} (read number/title/scale from the title block; infer \
  discipline from content, e.g. "architectural", "structural", "civil", "electrical", "mechanical", "plumbing").

- "schedules": one row per schedule TABLE entry of any kind (footing schedule, door/window schedule, room/ \
  space/finish schedule, fixture schedule, luminaire schedule, pipe/duct schedule, equipment schedule, manhole/ \
  structure schedule -- whatever tabular catalogue exists on this sheet). Parse every schedule table \
  COMPLETELY, one row per type/mark: {"type", "mark", "properties", "reliability": "HIGH"}. "properties" is a \
  short human-readable string of that row's other columns -- size, rating, material, area, capacity, invert \
  level, whatever is listed. If a dimension, area, or level value is in the row (SF, m2, mm, ft-in, RL=, IL=, \
  CL=), keep it verbatim in "properties" -- exact printed numbers here are the best possible answer to a later \
  question and avoid any need for geometry math.

- "notes": general/sheet notes blocks: {"category", "text", "reliability": "HIGH"}. ALSO include, as its own \
  row here, ANY dimension, size, area, level, or rating value printed directly next to a named/tagged element \
  on a PLAN, SECTION, or ELEVATION view (not in a schedule) -- this is extremely common and is the single most \
  reliable source for a later "what's the X of Y" question. Examples, follow this pattern for whatever the \
  drawing actually shows: {"category": "area", "text": "LOBBY: 850 SF"}, {"category": "dimension", "text": \
  "PATIO: 12'-0\\" x 10'-0\\""}, {"category": "dimension", "text": "DOOR D01: 3'-0\\" WIDE"}, {"category": \
  "level", "text": "MH3: IL=45.20"}. Capture every one you can find -- do not skip these because they seem \
  minor; they are exactly what "dimension of X" / "how big is Y" questions need.

- "instance_targets": one entry per COUNTABLE, REPEATED thing that appears as text/tag directly placed on this \
  sheet if it's a PLAN view (not a schedule row, not description prose) -- so "how many X" / "where are the X" \
  can be answered exactly, for absolutely any category of tagged or labeled element. Cover BOTH:
  (a) A schedule's marks placed as tags on this plan (e.g. F10/F12/F14 footings, D01-D06 doors, MH1-MH5 \
      manholes, C1/C2 columns) -- `pattern` matches the mark exactly as printed on the plan.
  (b) No schedule at all, but a repeated TYPE label is written directly on this plan (very common for rooms on \
      residential/commercial sets -- "BEDROOM 1", "KITCHEN", "OFFICE 204" -- but apply the same idea to ANY \
      discipline: repeated equipment labels, repeated structural callouts, repeated road-furniture labels, \
      whatever repeats on this specific sheet) -- propose a pattern matching the type, e.g. \
      "BEDROOM\\\\s*\\\\d*", so instances are counted/located even with nothing backing them in a schedule.
  Propose an instance_target for EVERY distinct repeated tag/label you can identify -- do not limit yourself to \
  one or two obvious categories; a single sheet can have many (rooms AND doors AND windows AND structural \
  marks, all as separate instance_targets). Only propose these if this sheet is a plan view. Each entry: \
  {"pattern" (a Python regex), "exclude_bboxes" (list of [x0,y0,x1,y1] regions on this sheet to exclude -- a \
  schedule table, legend, or title block that would otherwise double-count a definition as a placed instance; \
  empty list if there's nothing to exclude)}.

- "markdown": a thorough markdown summary of this sheet, as a single string -- title, discipline, scale, a \
  prose description of everything shown (not just a one-liner -- describe the layout, what's adjacent to what, \
  any labeled elements, any callouts), and its schedules/notes rendered as tables. This is for retrieval, not \
  for counting. End it with a "## Coordinate hints" section listing every named/tagged element visible on a \
  plan (room, door, staircase, fixture, structural mark, civil structure -- anything with a label) with its \
  approximate bbox and any printed value shown right next to it, e.g. "- LOBBY: near [120, 340, 180, 355], \
  AREA: 850 SF", "- STAIR 1: near [400, 200, 440, 260]", "- D01: near [60, 500, 80, 520], WIDTH: 3'-0\\"". This \
  is what lets later questions -- area, dimension, distance between two elements -- be answered by locating the \
  element(s) first. Be exhaustive here; omit the section only if this sheet genuinely has no such labels (e.g. \
  a pure notes/schedule sheet).

Never invent a value -- everything above must be read directly from what you're given. Store counts/marks/ \
values exactly as they literally appear on the sheet. If this sheet has no schedules, isn't a taggable plan, \
or has no notable callouts, return an empty list for the relevant key rather than guessing. Output strictly \
valid JSON, no commentary."""

VISION_ADDENDUM = """

You are ALSO given a rendered image of this exact sheet. Use it for anything that isn't captured as clean \
text -- most importantly graphical dimension strings (extension lines + arrows + a printed number), symbols, \
and spatial layout/adjacency. Read the image carefully for any dimension, size, or label that the text blocks \
alone wouldn't reveal. IMPORTANT: any value you read from the image that is NOT also confirmed in the given \
text blocks must be tagged "reliability": "MEDIUM", never "HIGH" -- it's a visual read, not exact extracted \
text. Never invent a digit or value you can't clearly read off the image; if a dimension is illegible, skip it \
rather than guessing."""


def call_sheet_extraction(sheet_payload: dict, route: str, image_path: str | None) -> dict:
    system_prompt = EXTRACTION_SYSTEM_PROMPT
    content: list[dict] = [{"type": "text", "text": json.dumps(sheet_payload)}]

    if route == "vision" and image_path and Path(image_path).exists():
        system_prompt = system_prompt + VISION_ADDENDUM
        b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
        model = config.CHAT_MODEL
    else:
        model = config.CLASSIFY_MODEL

    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
    )
    return json.loads(response.choices[0].message.content)


def embed_text(text: str) -> list[float]:
    response = client.embeddings.create(model=config.EMBEDDING_MODEL, input=text)
    return response.data[0].embedding


# ---------------------------------------------------------------------------
# Step 5e — provenance validation (the vendored skill calls this mandatory:
# confirms every atomic fact's tokens actually appear on its cited sheet,
# relocating rows that got attributed to the wrong sheet).
# ---------------------------------------------------------------------------
def run_provenance_validation(
    doc_dir: Path, file_map: dict, numbers_by_idx: dict, structured_path: Path, sqlite_out: Path
) -> int:
    text_dir = doc_dir / "sheet_text"
    text_dir.mkdir(exist_ok=True)
    for idx, number in numbers_by_idx.items():
        extraction_path = Path(file_map[idx]["extraction_path"])
        data = json.loads(extraction_path.read_text())
        safe_name = number.replace("/", "_").replace(" ", "_")
        (text_dir / f"{safe_name}.txt").write_text(data.get("all_text", ""), encoding="utf-8")

    fields_path = doc_dir / "provenance_fields.json"
    fields_path.write_text(
        json.dumps({"schedules": ["mark", "properties", "source_sheet"], "notes": ["category", "text", "source_sheet"]})
    )
    report_path = doc_dir / "provenance_report.json"

    try:
        _run_script(
            [
                str(config.SCRIPTS_DIR / "validate_provenance.py"),
                str(structured_path),
                "--textdir", str(text_dir),
                "--fields", str(fields_path),
                "--apply",
                "-o", str(report_path),
            ]
        )
    except RuntimeError:
        return 0

    report = json.loads(report_path.read_text()) if report_path.exists() else []
    relocations = sum(1 for row in report if str(row.get("action", "")).startswith("RELOCATE"))

    validated_path = Path(str(structured_path).replace(".json", "_validated.json"))
    if relocations and validated_path.exists():
        run_build_db(validated_path, sqlite_out)

    return relocations


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
            "image_path": sheet.get("image_path"),
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
    classifications = classify_sheets(sheets_payload)

    sheets_rows: list[dict] = []
    schedules_rows: list[dict] = []
    notes_rows: list[dict] = []
    instances_rows: list[dict] = []
    markdown_entries: list[tuple[str, str]] = []
    sheet_files: dict[str, str] = {}
    sheet_images: dict[str, str] = {}
    numbers_by_idx: dict[int, str] = {}

    for sp in sheets_payload:
        idx = sp["sheet_index"]
        file_info = file_map[idx]
        sheet_type = classifications.get(idx, "other")
        route = route_for(sheet_type)

        try:
            result = call_sheet_extraction(sp, route, file_info.get("image_path"))
        except Exception as e:  # noqa: BLE001 - one sheet's failure shouldn't kill the whole upload
            print(f"WARNING: extraction failed for sheet {idx}: {e}", file=sys.stderr)
            result = {}

        sheet_meta = result.get("sheet") or {}
        number = sheet_meta.get("number") or file_info["stem"]
        numbers_by_idx[idx] = number
        sheet_files[number] = file_info["pdf_path"]
        if file_info.get("image_path"):
            sheet_images[number] = file_info["image_path"]

        sheets_rows.append(
            {
                "number": number,
                "title": sheet_meta.get("title"),
                "discipline": sheet_meta.get("discipline"),
                "scale": sheet_meta.get("scale"),
                "source_sheet": number,
            }
        )

        for row in result.get("schedules", []):
            schedules_rows.append(
                {
                    "type": row.get("type"),
                    "mark": row.get("mark"),
                    "properties": row.get("properties"),
                    "source_sheet": number,
                    "reliability": row.get("reliability", "HIGH"),
                }
            )

        for row in result.get("notes", []):
            notes_rows.append(
                {
                    "category": row.get("category"),
                    "text": row.get("text"),
                    "source_sheet": number,
                    "reliability": row.get("reliability", "HIGH"),
                }
            )

        for target_idx, target in enumerate(result.get("instance_targets", [])):
            pattern = target.get("pattern")
            if not pattern:
                continue
            out_path = split_dir / f"instances_{idx}_{target_idx}.json"
            try:
                extracted = run_extract_instances(
                    Path(file_info["extraction_path"]),
                    pattern,
                    number,
                    target.get("exclude_bboxes", []),
                    out_path,
                )
            except (RuntimeError, json.JSONDecodeError):
                continue
            for r in extracted:
                instances_rows.append(
                    {
                        "tag": r["tag"],
                        "x": r["x"],
                        "y": r["y"],
                        "source_sheet": number,
                        "reliability": "HIGH",
                    }
                )

        content = result.get("markdown")
        if content:
            markdown_entries.append((number, content))

    (d / "sheet_files.json").write_text(json.dumps(sheet_files, indent=2))
    (d / "sheet_images.json").write_text(json.dumps(sheet_images, indent=2))

    structured = {
        "sheets": sheets_rows,
        "schedules": schedules_rows,
        "instances": instances_rows,
        "notes": notes_rows,
    }
    structured_path = d / "structured.json"
    structured_path.write_text(json.dumps(structured, indent=2))
    run_build_db(structured_path, config.sqlite_path(doc_id))

    provenance_corrections = run_provenance_validation(
        d, file_map, numbers_by_idx, structured_path, config.sqlite_path(doc_id)
    )

    # Per-sheet markdown -> embed -> store in the `chunks` table (simple in-SQLite vector store).
    conn = db.get_conn(doc_id)
    # build_db.py skips creating a table when its list is empty (e.g. no schedules on this
    # set) -- make sure sheets/schedules/instances/notes always exist so query_sql gets an
    # empty result instead of a "no such table" error.
    db.ensure_core_tables(conn)
    db.ensure_chunks_table(conn)
    markdown_dir = d / "markdown"
    markdown_dir.mkdir(exist_ok=True)
    for number, content in markdown_entries:
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
        "provenance_corrections": provenance_corrections,
    }
