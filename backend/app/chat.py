import importlib.util
import json
import re
import subprocess
import sys

from . import config, db, ingest

# Reuse the vendored skill's imperial-scale parser (e.g. `1/8"=1'-0"` -> a `1:N` ratio)
# instead of re-deriving it -- see process_drawing.py's own scale-detection logic.
_spec = importlib.util.spec_from_file_location("process_drawing", config.SCRIPTS_DIR / "process_drawing.py")
_process_drawing = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_process_drawing)

CHAT_SYSTEM_PROMPT = """You are answering questions about a construction drawing set that has already been \
ingested into three things:
1. A SQLite database (tables: sheets, schedules, instances, notes). `instances` carries one row per tag \
   PLACED on a plan with x/y coordinates -- it is the ONLY source of truth for counts and locations. Never \
   estimate, guess, or eyeball a count yourself; always get it from `instances` via query_sql.
2. Per-sheet markdown summaries, searchable with search_markdown -- prose: specs, notes, assemblies, general \
   description of a sheet, and (where present) a "Coordinate hints" section naming rooms/spaces and their \
   approximate location on that sheet.
3. The original per-sheet PDFs, queryable on demand with query_pdf -- for areas/dimensions not already in the \
   database.

Routing:
- Counts / "how many" / locations / "where are" -> query_sql against `instances` (join `schedules` for \
  type-level facts like ratings or material).
- "What does the spec say" / notes / assemblies / general description -> search_markdown.
- Areas, dimensions, or anything not already in the database -> query_pdf. Its results are reliability MEDIUM \
  (scaled/derived on demand) -- never state them as hard fact, always hedge.

Playbook for "area/dimension of a named thing" (e.g. "square footage of the lobby"), IN THIS ORDER, do not skip \
straight to geometry:
1. query_sql first: `SELECT * FROM schedules WHERE mark LIKE '%<name>%' OR properties LIKE '%<name>%'` and \
   `SELECT * FROM notes WHERE text LIKE '%<name>%'`. Room/space schedules and printed area callouts (e.g. \
   "LOBBY 850 SF" written right on the plan) are captured here at HIGH reliability when the drawing shows them \
   -- this is exact, already-read text, not a computed estimate, and is very often where the real answer is.
2. If step 1 finds nothing, search_markdown for the name -- a "Coordinate hints" entry may give you the sheet \
   and an approximate bbox (and sometimes the area directly, if it was printed on the plan).
3. Only if you still don't have a value: call query_pdf subcommand "text" (no bbox) on that sheet to find the \
   label's exact location, then FIRST look for a nearby number followed by SF/SQ FT/M2 in the returned text -- \
   printed area callouts are common and far more reliable than computing area from geometry. Only if no such \
   text exists, try subcommand "polygons" with a bbox around the location as a last resort, and if that comes \
   back with 0 polygons (common -- architectural walls are frequently drawn as separate wall segments rather \
   than one closed room-boundary path, so polygon detection can legitimately find nothing), try subcommand \
   "dimensions" in the same bbox for annotated room dimensions (e.g. "20'-0\" x 15'-0\"") instead of giving up.
4. Do NOT ask the user for the drawing scale -- query_pdf resolves it automatically from the sheets table or the \
   sheet's own title block and reports what it used (or that none was found) in the result. If the tool result \
   says no scale was found, say that plainly in one sentence; do not loop asking the user for it.
5. Only after actually trying this whole chain, if nothing is found anywhere, say so directly and briefly -- do \
   not pad the answer with a paragraph of caveats.

Always cite the source_sheet and reliability of every fact in your final answer. Prefer the database, then the \
markdown, then the PDF, in that order, when more than one could answer."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_sql",
            "description": (
                "Run a read-only SELECT against the drawing database. Tables: "
                "sheets(number,title,discipline,scale,source_sheet), "
                "schedules(type,mark,properties,source_sheet,reliability), "
                "instances(tag,x,y,source_sheet,reliability) -- one row per tag placed on a plan, "
                "the only source of truth for counts/locations, "
                "notes(category,text,source_sheet,reliability)."
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
