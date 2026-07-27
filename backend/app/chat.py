import importlib.util
import json
import math
import re
import subprocess
import sys

from . import config, db, ingest


def _load_vendored(name: str):
    """Import one of the vendored skill's scripts as a module, so we call its functions
    directly instead of re-deriving the same logic (scale parsing, unit conversion)."""
    spec = importlib.util.spec_from_file_location(name, config.SCRIPTS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_process_drawing = _load_vendored("process_drawing")
_query_drawing = _load_vendored("query_drawing")

CHAT_SYSTEM_PROMPT = """You are answering questions about a set of CAD/construction drawings that has already \
been ingested. This could be any type of drawing set -- a house, an apartment building, a shop, a skyscraper, \
a road/civil plan, an electrical/mechanical layout, anything -- and the question could be about anything on \
it: counts, locations, dimensions, areas, ratings, materials, or the distance between two elements. Don't \
assume a narrow set of question types; work out what's actually being asked and find it.

What you have:
1. A SQLite database (tables: sheets, schedules, instances, notes). `instances` carries one row per tag/label \
   PLACED on a plan with x/y coordinates -- rooms, doors, footings, manholes, or whatever this specific set \
   tags -- it is the ONLY source of truth for counts and locations. Never estimate, guess, or eyeball a count \
   yourself; always get it from `instances` via query_sql. `schedules` and `notes` hold exact printed values \
   (sizes, ratings, materials, areas, dimensions, levels) at HIGH reliability -- always check these before \
   trying to derive a value from geometry.
2. Per-sheet markdown summaries, searchable with search_markdown -- prose description of each sheet, plus a \
   "Coordinate hints" section naming labeled elements and their approximate location on that sheet.
3. The original per-sheet PDFs, queryable on demand with query_pdf -- for anything not already in the \
   database (an area, a dimension, raw text/tables in a region).
4. measure_distance -- computes the real-world distance between two points on the same sheet, once you know \
   both points' (x,y).

Routing:
- Counts / "how many" / locations / "where is/are" -> query_sql against `instances`.
- Distance / "how far" / "distance between X and Y" -> see the distance playbook below.
- Any attribute of a named thing (dimension, area, length, width, rating, material, level, capacity) -> see \
  the attribute playbook below.
- General description / "what does it say about" / specs / assemblies -> search_markdown.

Playbook for "<attribute> of <named thing>" (e.g. "square footage of the lobby", "length of door D01", \
"dimension of the patio"), IN THIS ORDER, do not skip straight to geometry:
1. query_sql first: `SELECT * FROM schedules WHERE mark LIKE '%<name>%' OR properties LIKE '%<name>%'` and \
   `SELECT * FROM notes WHERE text LIKE '%<name>%'`. Printed callouts (areas, dimensions, ratings, levels) are \
   captured here at HIGH reliability when the drawing shows them -- exact, already-read text, and very often \
   where the real answer already is.
2. If step 1 finds nothing, search_markdown for the name -- a "Coordinate hints" entry gives you the sheet and \
   an approximate bbox (sometimes the value directly, if it was printed on the plan).
3. Only if you still don't have a value: call query_pdf subcommand "text" (no bbox) on that sheet to find the \
   label's exact location, then FIRST look for a nearby printed value (a number with a unit -- SF, m2, mm, \
   ft-in, a level like RL=/IL=) in the returned text -- far more reliable than computing it from geometry. \
   Only if no such text exists, try subcommand "polygons" (area) or "dimensions" (annotated lengths) with a \
   bbox around the location as a last resort. If "polygons" comes back with 0 results (common -- walls are \
   often drawn as separate segments, not one closed boundary), try "dimensions" in the same bbox before giving \
   up.
4. Do NOT ask the user for the drawing scale -- query_pdf and measure_distance resolve it automatically and \
   report what they used (or that none was found). If a tool result says no scale was found, say that plainly \
   in one sentence; do not loop asking the user for it.
5. Only after actually trying this whole chain, if nothing is found anywhere, say so directly and briefly -- do \
   not pad the answer with a paragraph of caveats.

Playbook for "distance between A and B" / "how far is X from Y":
1. Find each element's (x,y) and sheet -- query_sql against `instances` first (`SELECT tag,x,y,source_sheet \
   FROM instances WHERE tag LIKE '%A%'`, same for B); if either isn't in `instances`, locate it via \
   search_markdown's "Coordinate hints" or a query_pdf "text" call, and use the bbox center as its (x,y).
2. Both points must be on the SAME sheet (the same drawing/coordinate space) to measure between them -- if they \
   aren't, say so; don't estimate across sheets.
3. Call measure_distance with that sheet_number and both (x,y) pairs. It resolves the scale itself and returns \
   a real-world distance -- don't compute it yourself from raw PDF-point coordinates, the unit conversion needs \
   the drawing's scale applied correctly.
4. Report the distance with its reliability (MEDIUM if a scale was resolved, otherwise say no scale was found \
   and give the raw PDF-point figure only if asked).

Always cite the source_sheet and reliability of every fact in your final answer. Prefer the database, then the \
markdown, then the PDF/measurement tools, in that order, when more than one could answer."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_sql",
            "description": (
                "Run a read-only SELECT against the drawing database. Tables: "
                "sheets(number,title,discipline,scale,source_sheet), "
                "schedules(type,mark,properties,source_sheet,reliability), "
                "instances(tag,x,y,source_sheet,reliability) -- one row per labeled/tagged thing placed on a "
                "plan (room, door, footing, manhole, or whatever this set tags), the only source of truth for "
                "counts/locations, "
                "notes(category,text,source_sheet,reliability) -- includes printed dimension/area/level "
                "callouts, use LIKE on `text` to find one by name."
            ),
            "parameters": {
                "type": "object",
                "properties": {"sql": {"type": "string", "description": "A single SELECT statement."}},
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_markdown",
            "description": "Semantic search over per-sheet markdown summaries (prose: specs, notes, assemblies). Not for counts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "description": "default 4"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_pdf",
            "description": (
                "On-demand query of the original sheet PDF for a value not already in the database "
                "(area, annotated dimension, raw table, region text, page scale). Result is reliability "
                "MEDIUM (scaled/derived) -- hedge it, never state it as fact. For polygons/dimensions, the "
                "drawing scale is resolved automatically (sheets table, then the sheet's own title block) -- "
                "do not ask the user for it; pass `scale` yourself only to override."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sheet_number": {"type": "string", "description": "The sheet's `number` from the sheets table."},
                    "subcommand": {
                        "type": "string",
                        "enum": ["polygons", "text", "dimensions", "tables", "page-info"],
                    },
                    "bbox": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "[x0,y0,x1,y1] in PDF points; omit for full page.",
                    },
                    "scale": {"type": "string", "description": "e.g. '1:100' -- overrides auto-resolution, usually not needed."},
                },
                "required": ["sheet_number", "subcommand"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "measure_distance",
            "description": (
                "Real-world straight-line distance between two points on the SAME sheet, given their (x,y) in "
                "PDF points (get these from query_sql's instances.x/instances.y, or a query_pdf 'text' bbox "
                "center). Scale is auto-resolved the same way as query_pdf -- don't compute this yourself from "
                "raw coordinates. Reliability MEDIUM if a scale was resolved, otherwise the result says so and "
                "only the raw PDF-point distance is available."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sheet_number": {"type": "string", "description": "The sheet's `number` from the sheets table."},
                    "x1": {"type": "number"},
                    "y1": {"type": "number"},
                    "x2": {"type": "number"},
                    "y2": {"type": "number"},
                },
                "required": ["sheet_number", "x1", "y1", "x2", "y2"],
            },
        },
    },
]

MAX_TOOL_ROUNDS = 8


def _scale_string_to_ratio(text: str) -> str | None:
    """Convert a title-block scale string (imperial or metric) to a `1:N` ratio, or None."""
    if not text:
        return None
    m = re.search(_process_drawing.IMPERIAL_PATTERN, text)
    if m:
        factor = _process_drawing._imperial_to_factor(m.group(1), m.group(2), m.group(3))
        if factor:
            return f"1:{factor}"
    m2 = re.search(r"1\s*:\s*(\d+)", text)
    if m2:
        return f"1:{m2.group(1)}"
    return None


def _resolve_scale(conn, sheet_files: dict, sheet_number: str) -> str | None:
    """Look up a sheet's scale: first the `sheets` table (title block, parsed at ingest),
    then fall back to a live page-info scan of that sheet's own PDF."""
    row = conn.execute("SELECT scale FROM sheets WHERE number = ?", (sheet_number,)).fetchone()
    if row and row["scale"]:
        ratio = _scale_string_to_ratio(row["scale"])
        if ratio:
            return ratio

    pdf_path = sheet_files.get(sheet_number)
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
        ratio = _scale_string_to_ratio(cand.get("text", ""))
        if ratio:
            return ratio
    return None


def _query_pdf(conn, sheet_files: dict, args: dict) -> dict:
    pdf_path = sheet_files.get(args["sheet_number"])
    if not pdf_path:
        return {"error": f"unknown sheet_number {args['sheet_number']!r}"}

    subcommand = args["subcommand"]
    scale = args.get("scale")
    scale_auto_resolved = False
    if not scale and subcommand in ("polygons", "dimensions"):
        scale = _resolve_scale(conn, sheet_files, args["sheet_number"])
        scale_auto_resolved = scale is not None

    cmd = [sys.executable, str(config.SCRIPTS_DIR / "query_drawing.py"), subcommand, pdf_path]
    if args.get("bbox"):
        cmd += ["--bbox", *[str(v) for v in args["bbox"]]]
    if scale:
        cmd += ["--scale", scale]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return {"error": result.stderr.strip()}

    data = json.loads(result.stdout)
    data["reliability"] = "MEDIUM"
    data["note"] = "on-demand PDF geometry/text query, not curated into the database -- verify before stating as fact"
    if subcommand in ("polygons", "dimensions"):
        if scale:
            data["scale_used"] = scale
            data["scale_source"] = "auto-resolved" if scale_auto_resolved else "provided"
        else:
            data["scale_used"] = None
            data["scale_note"] = "no scale could be resolved from the sheets table or the sheet's title block -- state this to the user, do not guess"
    if subcommand == "polygons" and data.get("polygon_count") == 0:
        data["next_step_hint"] = (
            "No closed polygon in this bbox -- normal when walls are drawn as separate segments rather than "
            "one closed room-boundary path. Try subcommand 'text' in the same bbox for a printed area callout "
            "(e.g. '850 SF'), or subcommand 'dimensions' for annotated room dimensions, before giving up."
        )
    return data


def _mm_to_feet_inches(mm: float) -> str:
    total_inches = mm / 25.4
    feet = int(total_inches // 12)
    inches = total_inches - feet * 12
    return f"{feet}'-{inches:.1f}\""


def _measure_distance(conn, sheet_files: dict, args: dict) -> dict:
    sheet_number = args["sheet_number"]
    x1, y1, x2, y2 = args["x1"], args["y1"], args["x2"], args["y2"]
    dist_pts = math.hypot(x2 - x1, y2 - y1)

    scale = _resolve_scale(conn, sheet_files, sheet_number)
    result = {"sheet_number": sheet_number, "distance_pts": round(dist_pts, 2)}
    if not scale:
        result["scale_used"] = None
        result["reliability"] = "LOW"
        result["note"] = "no scale could be resolved -- only the raw PDF-point distance is available, do not convert it yourself"
        return result

    m = re.match(r"1\s*:\s*(\d+)", scale)
    if not m:
        result["scale_used"] = scale
        result["reliability"] = "LOW"
        result["note"] = f"scale {scale!r} could not be parsed into a ratio"
        return result

    mm_per_pt = int(m.group(1)) * _query_drawing.MM_PER_POINT
    dist_mm = dist_pts * mm_per_pt
    result.update(
        {
            "scale_used": scale,
            "distance_mm": round(dist_mm, 1),
            "distance_m": round(dist_mm / 1000, 3),
            "distance_ft_in": _mm_to_feet_inches(dist_mm),
            "reliability": "MEDIUM",
            "note": "derived from drawn coordinates and the resolved scale -- verify before stating as fact",
        }
    )
    return result


def _dispatch_tool(conn, sheet_files: dict, name: str, args: dict) -> dict:
    if name == "query_sql":
        try:
            return {"rows": db.run_readonly_sql(conn, args["sql"])}
        except Exception as e:  # noqa: BLE001 - surfaced to the model, not the user
            return {"error": str(e)}
    if name == "search_markdown":
        embedding = ingest.embed_text(args["query"])
        return {"matches": db.search_chunks(conn, embedding, top_k=args.get("top_k", 4))}
    if name == "query_pdf":
        return _query_pdf(conn, sheet_files, args)
    if name == "measure_distance":
        return _measure_distance(conn, sheet_files, args)
    return {"error": f"unknown tool {name!r}"}


def answer_question(doc_id: str, question: str, history: list[dict] | None = None) -> dict:
    sheet_files_path = config.doc_dir(doc_id) / "sheet_files.json"
    sheet_files = json.loads(sheet_files_path.read_text()) if sheet_files_path.exists() else {}

    conn = db.get_conn(doc_id)
    db.ensure_chunks_table(conn)

    # Prior turns are plain text only (no tool-call scaffolding replayed) -- this is what
    # lets a follow-up like "i don't know" land in context instead of as a fresh, unrelated
    # question with no memory of what was just asked.
    messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
    for turn in history or []:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})
    tool_calls_made: list[str] = []

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            response = ingest.client.chat.completions.create(
                model=config.CHAT_MODEL,
                messages=messages,
                tools=TOOLS,
            )
            msg = response.choices[0].message

            if not msg.tool_calls:
                return {"answer": msg.content or "", "tool_calls": tool_calls_made}

            messages.append(msg.model_dump(exclude_unset=True))
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                tool_calls_made.append(tc.function.name)
                result = _dispatch_tool(conn, sheet_files, tc.function.name, args)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})

        return {
            "answer": "I couldn't settle on an answer within the tool-call budget for this question.",
            "tool_calls": tool_calls_made,
        }
    finally:
        conn.close()
