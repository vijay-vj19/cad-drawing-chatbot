# Vendor scripts used: none -- pure AI judgement (Step 5 per-sheet analysis)
import base64
import json
from pathlib import Path

from openai import OpenAI

from .. import config
from .classify import route_for

client = OpenAI(api_key=config.OPENAI_API_KEY)

# Per-type templates, condensed from references/drawing_types.md. Section headings are kept
# verbatim where later steps depend on them: Step 6 (cross-reference graph) reads every
# sheet's "## Cross-references on this sheet" section by this exact heading.
TEMPLATES = {
    "general_arrangement": """## View extent
## Grid and setout
## Spaces / rooms / zones
## Building elements (perspective-tilted)
## Materials / specifications called out
## Cross-references on this sheet
## Notes / annotations""",
    "section_view": """## What this section shows
## Levels and dimensions
## Construction shown (perspective-tilted)
## Materials and specifications
## Cross-references on this sheet
## Notes / annotations""",
    "elevation": """## What this elevation shows
## Facade composition / finishes
## Heights and setouts
## Openings
## Cross-references on this sheet
## Notes / annotations""",
    "detail": """## What's being detailed
## Parent sheet(s)
## Layered construction
## Critical dimensions
## Specifications referenced
## Cross-references on this sheet""",
    "schedule_sheet": """## Schedule type
## Row count
## Headers
## Entries (markdown table; full for <=30 rows, else first 10 + last 10)
## Cross-references on this sheet""",
    "general_notes": """## Notes (verbatim, grouped by category as they appear)
## Specifications referenced
## Standards referenced""",
    "legend": """## Symbols defined
## Abbreviations
## Notes about symbol usage""",
    "single_line_diagram": """## System represented
## Source(s) of supply
## Distribution structure (node by node: upstream, protective device, cable, downstream)
## Capacities and ratings
## Cross-references on this sheet""",
    "cover_sheet": """## Project information
## Drawing register
## Location / context
## Notes / disclaimers""",
    "coordination_drawing": """## Disciplines overlaid
## Coordination zones
## Resolved clashes
## Open coordination items
## Cross-references on this sheet""",
    "other": """## Justification (why this didn't fit any other type)
## Content (best-effort exhaustive description, perspective-tilted)
## Cross-references on this sheet""",
}

# The two sections every per-sheet markdown must end with, per drawing_types.md -- this is
# what turns a static description into "what this sheet can answer precisely", which Query
# mode (Q1) reads to route a question to the right sheet.
UNIVERSAL_SECTIONS = """## Answerable from this drawing
**Precisely (via query_drawing.py):** <question types this sheet can answer exactly>
**By inspection of the rendered PNG (vision required, less precise):** <question types>
**Not answerable from this sheet -- refer:** <question type> -> <other sheet/source>

## Coordinate hints for query-time extraction
Page size: <width_pts> x <height_pts> pts (origin top-left)
Region | bbox (x0, y0, x1, y1)
Title block | ...
Drawing area (excludes title) | ...
<Named feature> | ..."""

STRUCTURED_SCHEMA = """Alongside the markdown, also return structured facts pulled from this sheet, per this
schema (references/schema_guidance.md):

- "schedules": one row per schedule TABLE entry (type catalogue, not placed instances):
  {"type", "mark", "properties", "reliability"}. Parse every schedule table completely --
  it is pre-structured data sitting on the sheet, the highest-confidence content available.
- "notes": one row per general/spec note AND per dimension/area/level value printed next to
  a named element on a plan/section/elevation (not in a schedule): {"topic", "text",
  "reliability"}. Store any count as an explicit integer elsewhere, never inline notation
  like "F10 x2" inside a note's text.
- "relationships": connective facts STATED in text only (e.g. "MSB feeds DB-L1", "P-1
  discharges to T-2") -- never inferred from two things merely being near each other on the
  page: {"from_entity", "relation", "to_entity", "reliability"}.
- "instance_targets": one entry per distinctly labelled thing placed on this sheet if it's a
  PLAN view (a schedule's marks placed as tags, e.g. F10/D01/MH1; every room/space label,
  even ones appearing exactly once; any other distinctly labelled element) -- this seeds
  Step 5b's coordinate extraction. Each: {"pattern" (a Python regex matching the tag as
  printed), "entity_type", "exclude_bboxes" (regions to exclude -- a schedule table, legend,
  or title block that would otherwise double-count a definition as a placed instance)}.

Every schedules/notes/relationships row needs a "reliability": "HIGH" (read directly from
text) or "MEDIUM" (read from the image, not confirmed in text) -- MEDIUM if anything came
from the picture rather than the extracted text. Never invent a value; if this sheet has
none of a given kind, return an empty list for it."""


def _build_prompt(drawing_type: str, perspective: str) -> str:
    template = TEMPLATES.get(drawing_type, TEMPLATES["other"])
    return f"""You are analysing ONE sheet of a construction drawing set from a {perspective} perspective.
This could be any discipline or building type -- read what's actually shown, don't assume.

You are given this sheet's extracted text blocks (with bounding boxes) and title-block
candidates. Write a thorough per-sheet markdown analysis using this template for a
{drawing_type} sheet:

# Sheet <ID> -- <Title> ({perspective} perspective)

## Title block
Sheet ID: <...>  Title: <...>  Drawing Type: {drawing_type}  Discipline: <...>
Scale: <...>  Revision: <...>

{template}

{UNIVERSAL_SECTIONS}

{STRUCTURED_SCHEMA}

Return strictly valid JSON, no commentary, matching:
{{"sheet": {{"sheet_id", "title", "discipline", "scale", "rev"}},
 "schedules": [...], "notes": [...], "relationships": [...], "instance_targets": [...],
 "markdown": "<the full markdown document as one string>"}}"""


def analyze_sheet(sheet: dict, drawing_type: str, perspective: str) -> dict:
    """Step 5 (+ the structured-facts side of Step 5d, combined -- see module docstring
    equivalent above): one LLM call per sheet, routed text-only or vision per Step 3's
    classification, using the type-specific template."""
    extraction = json.loads(Path(sheet["extraction_path"]).read_text())
    payload = {
        "title_block_candidates": extraction.get("title_block_candidates", [])[:20],
        "text_blocks": extraction.get("text_blocks", [])[:800],
        "width_pts": extraction.get("width_pts"),
        "height_pts": extraction.get("height_pts"),
    }
    content = [{"type": "text", "text": json.dumps(payload)}]

    route = route_for(drawing_type)
    model = config.CHEAP_MODEL
    if route == "vision" and sheet.get("image_path") and Path(sheet["image_path"]).exists():
        model = config.STRONG_MODEL
        b64 = base64.b64encode(Path(sheet["image_path"]).read_bytes()).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})

    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _build_prompt(drawing_type, perspective)},
            {"role": "user", "content": content},
        ],
    )
    return json.loads(response.choices[0].message.content)
