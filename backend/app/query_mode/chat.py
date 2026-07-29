# Vendor scripts used: none -- the LLM tool-calling loop; tools.py is what touches scripts
import json

from openai import OpenAI

from .. import config, db
from . import tools

client = OpenAI(api_key=config.OPENAI_API_KEY)

# Paraphrased directly from SKILL.md's "Workflow -- Query mode" (Q1-Q6).
SYSTEM_PROMPT = """You are answering a follow-up question about a construction drawing set that has
already been indexed. Do not re-analyse the drawings -- everything you need was captured at
index time.

Q1 -- Route. Counts / "how many" / locations / "where are" / relationships / aggregations ->
the database. Geometry not already in the database (areas, annotated dimensions, region
text) -> query_pdf on the right source PDF. Use read_markdown(kind="index") for the drawing
register and to find which sheet answers a general/descriptive question; read_markdown
(kind="sheet", name=<sheet_id>) for that sheet's own write-up, including its "Answerable
from this drawing" and "Coordinate hints" sections; read_markdown(kind="concept", name=...)
for a concept/requirement/spec question (read kind="concept" with no name first for the
index of available concept pages).

Q2 -- Database query (preferred where it applies). Tables: sheets(sheet_id, title,
drawing_type, discipline, scale, rev, source_pdf); schedules(type, mark, properties,
source_sheet, reliability) -- the type catalogue; instances(tag, entity_type, x, y, sheet_id,
grid, properties, reliability) -- one row per physical thing placed on a plan, the only
source of truth for counts/locations. In instances, `tag` is the specific name/label exactly
as printed on the plan (e.g. "BEDROOM", "F10", "D01") -- filter on THIS with LIKE for a
"how many X" / "where is X" question. `entity_type` is only a broad category the model
assigned (e.g. "space", "door", "footing") -- too coarse for a specific name, don't filter on
it for a named thing. notes(topic, text, source_sheet, reliability);
relationships(from_entity, relation, to_entity, source_sheet, reliability) -- connections
stated in text only; context_notes(note, reliability, basis, what_to_verify, source_sheet)
-- qualitative vision observations; placeholders(entity_or_topic, what_is_missing, why,
suggested_sheet) -- an honest gap, not a value. Report the row's reliability and hedge
LOW/scaled values. If the fact is a placeholder, say so and route to query_pdf/the image
instead.

If a name search against `instances`/`schedules`/`notes` comes back empty, that does NOT mean
the thing doesn't exist -- it may just be labelled differently on the actual drawing than the
word the question used (e.g. a question about "bedroom" when the plan labels that space
"LOFT" or "STUDIO"). Before concluding something isn't there: browse the broader list of tags
on the relevant sheet (e.g. `SELECT DISTINCT tag FROM instances WHERE sheet_id = '<id>'`) or
read that sheet's markdown/text for a plausible match, and use your own judgement about
whether a differently-worded label answers the question. Only report "not found" after
actually checking -- and if you do find a plausible differently-named match, say so plainly
("there's no room labelled 'bedroom', but there is a 'LOFT', which commonly serves as a
sleeping area in this kind of plan").

Before reporting a COUNT of any room/tag type, check whether the rows span two sheets that
might be duplicate views of the SAME physical area -- e.g. a plain "Architectural Plan" and a
"Dimensioned Plan" of the same floor get tagged separately but show identical rooms, just
annotated differently. Query `sheets` for the titles/types of the sheet_ids involved; if two
look like the same layout shown twice, count each physical room once, not once per sheet it
appears on, and say so plainly in the answer.

Q3 -- Geometry query. Use the sheet's Coordinate hints (from read_markdown) to pick a bbox,
then call query_pdf: subcommand "polygons" for an area (scale resolves automatically from
the sheets table or the sheet's own title block -- do not ask the user for it); "tables" for
schedule contents; "text" for raw text in a region; "dimensions" for annotated dimensions;
"page-info" for a page-size/scale sanity check.

Q4 -- Validate against the visual. If a polygon area or other result looks wrong, or a value
only exists as a drawn dimension/graphic rather than text, call view_sheet_image as the true
last resort -- a live visual read of the rendered sheet, always MEDIUM reliability, always
hedged; it is not pixel-precise.

Q5 -- Answer with provenance and reliability. Cite the sheet and reliability once per answer,
not once per fact -- this should read like a person answering, not a report. Keep the final
answer to a sentence or two. Never show a raw PDF coordinate pair to the user -- those are
internal lookup keys; describe a location in plain words (sheet, nearby named things)
instead.

Q6 -- Don't fabricate scale. If no scale could be resolved and none is on the title block,
say that plainly and stop -- don't estimate an area/length anyway."""

# Light mode: no database, no per-sheet markdown was built at index time -- this document was
# small/simple enough that reading it directly at question time was judged cheaper than
# building the full structured pipeline (see index_mode/triage.py). Answer by reading sheets
# directly instead of querying a database.
LIGHT_SYSTEM_PROMPT = """You are answering a question about a small construction drawing set. No database or
per-sheet summaries were built for this set -- it was judged small/simple enough to answer
questions by reading the sheets directly instead. Use list_sheets to see what sheets exist,
query_pdf (subcommand "text" with no bbox reads a whole sheet's text; "dimensions"/"polygons"/
"tables" for specific values; "page-info" for scale) to read a sheet's content, and
view_sheet_image as a last resort for something only visible as a drawn graphic.

If you don't immediately see something the question asked about (e.g. a "bedroom") in a
sheet's text, that does NOT mean it doesn't exist -- the plan may label that space differently
(e.g. "LOFT" or "STUDIO" instead of "BEDROOM"). Before concluding something isn't there, check
the other sheets/rooms and use your own judgement about whether a differently-worded label
plausibly answers the question. Only report "not found" after actually looking -- and if you
find a plausible differently-named match, say so plainly ("there's no room labelled 'bedroom',
but there is a 'LOFT', which commonly serves as a sleeping area in this kind of plan").

Before reporting a count of any room/tag type, check whether it appears on what might be two
views of the SAME physical area -- e.g. a plain "Architectural Plan" and a "Dimensioned Plan"
of the same floor show identical rooms, just annotated differently. Read enough of both
sheets to tell whether they're the same layout before you count; if they are, count each
physical room once, not once per sheet it appears on. Say so plainly if you noticed and
corrected for this.

Answering rules: cite the sheet and reliability (HIGH for exact printed text, MEDIUM for
anything read off the rendered image or derived/scaled) once per answer, not once per fact.
Keep the final answer to a sentence or two, in plain language. Never show a raw PDF
coordinate pair to the user. Don't fabricate a scale -- if none is resolvable, say so plainly
rather than estimating an area/length anyway."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_sql",
            "description": (
                "Run a read-only SELECT against the drawing database (Q2). Text values (tags, "
                "notes, schedule marks) are stored exactly as printed on the drawing -- usually "
                "upper-case, sometimes with spacing quirks. Use LIKE with wildcards for matching "
                "a name, not an exact '=' comparison."
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
            "name": "read_markdown",
            "description": "Read a markdown artefact directly: the drawing-set index, one sheet's write-up, or a concept-wiki page (Q1).",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["index", "sheet", "concept"]},
                    "name": {"type": "string", "description": "sheet_id for kind=sheet, concept slug for kind=concept (omit for the concept index)."},
                },
                "required": ["kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_pdf",
            "description": "On-demand geometry/text query of a sheet's source PDF, for a value not already in the database (Q3).",
            "parameters": {
                "type": "object",
                "properties": {
                    "sheet_id": {"type": "string"},
                    "subcommand": {"type": "string", "enum": ["polygons", "text", "dimensions", "tables", "page-info"]},
                    "bbox": {"type": "array", "items": {"type": "number"}, "description": "[x0,y0,x1,y1] in PDF points; omit for full page."},
                    "scale": {"type": "string", "description": "e.g. '1:100' -- overrides auto-resolution, usually not needed."},
                },
                "required": ["sheet_id", "subcommand"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_sheet_image",
            "description": "Last-resort visual read of a sheet's rendered image for one specific question (Q4).",
            "parameters": {
                "type": "object",
                "properties": {
                    "sheet_id": {"type": "string"},
                    "question": {"type": "string"},
                },
                "required": ["sheet_id", "question"],
            },
        },
    },
]

# Light mode reuses query_pdf/view_sheet_image's exact schemas from TOOLS -- no database, no
# markdown, so query_sql/read_markdown are dropped and list_sheets is added.
LIGHT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_sheets",
            "description": "List every sheet in this drawing set (no classification has run -- these are just filenames; read a sheet's own text to identify what it actually is).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
] + [t for t in TOOLS if t["function"]["name"] in ("query_pdf", "view_sheet_image")]

MAX_TOOL_ROUNDS = 8


def _dispatch(conn, sheet_files: dict, sheet_images: dict, doc_dir, name: str, args: dict) -> dict:
    if name == "query_sql":
        return tools.query_sql(conn, args["sql"])
    if name == "read_markdown":
        return tools.read_markdown(doc_dir, args["kind"], args.get("name"))
    if name == "query_pdf":
        return tools.query_pdf(conn, sheet_files, args["sheet_id"], args["subcommand"], args.get("bbox"), args.get("scale"))
    if name == "view_sheet_image":
        return tools.view_sheet_image(sheet_images, args["sheet_id"], args["question"])
    if name == "list_sheets":
        return tools.list_sheets(doc_dir)
    return {"error": f"unknown tool {name!r}"}


def answer_question(doc_id: str, question: str, history: list[dict] | None = None) -> dict:
    doc_dir = config.doc_dir(doc_id)
    sheet_files = json.loads((doc_dir / "sheet_files.json").read_text()) if (doc_dir / "sheet_files.json").exists() else {}
    sheet_images = json.loads((doc_dir / "sheet_images.json").read_text()) if (doc_dir / "sheet_images.json").exists() else {}

    mode_path = doc_dir / "mode.json"
    light_mode = json.loads(mode_path.read_text())["mode"] == "light" if mode_path.exists() else False

    conn = None if light_mode else db.get_conn(doc_id)
    if conn is not None:
        db.ensure_schema(conn)

    system_prompt = LIGHT_SYSTEM_PROMPT if light_mode else SYSTEM_PROMPT
    tool_defs = LIGHT_TOOLS if light_mode else TOOLS

    messages = [{"role": "system", "content": system_prompt}]
    for turn in history or []:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})
    tool_calls_made: list[str] = []

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            response = client.chat.completions.create(model=config.STRONG_MODEL, messages=messages, tools=tool_defs)
            msg = response.choices[0].message

            if not msg.tool_calls:
                return {"answer": msg.content or "", "tool_calls": tool_calls_made}

            messages.append(msg.model_dump(exclude_unset=True))
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                tool_calls_made.append(tc.function.name)
                result = _dispatch(conn, sheet_files, sheet_images, doc_dir, tc.function.name, args)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})

        return {"answer": "I couldn't settle on an answer within the tool-call budget for this question.", "tool_calls": tool_calls_made}
    finally:
        if conn is not None:
            conn.close()
