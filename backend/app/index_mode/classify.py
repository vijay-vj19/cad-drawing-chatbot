# Vendor scripts used: none -- pure AI judgement (Step 3 classification + Step 3b scope gate)
import base64
import json
from pathlib import Path

from openai import OpenAI

from .. import config

client = OpenAI(api_key=config.OPENAI_API_KEY)

# Taxonomy verbatim from references/drawing_types.md -- "Use these types and only these
# types. If a sheet doesn't fit, use `other` and explain in the justification."
TAXONOMY = """
| Type | What it is | Common signals |
|---|---|---|
| general_arrangement | Floor plans, site plans, key plans. The default "layout" sheet. | Plan view, scale ~1:50-1:200, room labels, grid lines, dimensions. No section cuts, no schedule dominating the sheet. |
| section_view | Vertical cut through the building or a part of it. | Profile geometry, level annotations (FFL, ceiling, roof), labels like "Section 1-1". |
| elevation | External (or internal) view of a vertical surface. | Facade, no floor plan, materials/finishes called out on surfaces. |
| detail | Close-up of a specific assembly or junction. | Scale ~1:5-1:20, callouts referencing parent sheets. |
| schedule_sheet | Tabular data -- door, window, fixture, panel, finishes, equipment schedules. | Dominated by tables. |
| general_notes | Mostly text -- specifications, general notes, trade-specific notes. | High text density, low/no geometry. |
| legend | Defines the symbols used in this drawing set. | "LEGEND", "SYMBOLS", "ABBREVIATIONS" headings. |
| single_line_diagram | Schematic representation of a system (electrical, hydraulic, mechanical). | One-line schematic, no spatial scale, switchgear boxes. |
| cover_sheet | Project info, drawing register, sheet list, location plan. | Title-page styling, list of all drawings. |
| coordination_drawing | Multiple disciplines overlaid -- clash detection / services coordination. | Mixed line styles per discipline. |
| other | Anything else -- justify what it is. | - |
"""

CLASSIFICATION_PROMPT = """You are classifying a single construction drawing sheet.

Below is a rendered image of the sheet, and a JSON containing every piece of selectable
text on it (with bounding boxes) plus a text sample. Use both -- the text is reliable (read
the title block and any headings); the image shows you what's actually on the sheet.

Classify this sheet into exactly one drawing_type from the taxonomy below, and identify its
discipline (Architectural, Structural, Electrical, Mechanical, Hydraulic, Civil, Fire,
Communications, Landscape, etc). Provide a confidence (high/medium/low) and a one-sentence
justification grounded in what you see -- not what the sheet number suggests.
""" + TAXONOMY + """
Return JSON only, matching this schema:
{"sheet_id": "<from title block, or null>", "title": "<from title block, or null>",
 "discipline": "<...>", "drawing_type": "<one of the types above>",
 "confidence": "high|medium|low", "justification": "<one sentence>"}"""

# SKILL.md "Routing -- model + render per sheet": the classification is the routing key for
# Step 5's per-sheet analysis. Text-dominant types are already ~100% accurate from the
# vector text layer and skip the image; spatial/symbol/connectivity judgement needs it.
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


def route_for(drawing_type: str) -> str:
    return ROUTING.get(drawing_type, "vision")


def classify_sheet(sheet: dict) -> dict:
    """One LLM call per sheet (cheap model) -- image + extracted text -> type/discipline."""
    extraction = json.loads(Path(sheet["extraction_path"]).read_text())
    payload = {
        "stem": sheet["stem"],
        "title_block_candidates": extraction.get("title_block_candidates", [])[:20],
        "text_sample": extraction.get("all_text", "")[:1500],
    }
    content = [{"type": "text", "text": json.dumps(payload)}]
    if sheet.get("image_path") and Path(sheet["image_path"]).exists():
        b64 = base64.b64encode(Path(sheet["image_path"]).read_bytes()).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})

    response = client.chat.completions.create(
        model=config.CHEAP_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": CLASSIFICATION_PROMPT},
            {"role": "user", "content": content},
        ],
    )
    result = json.loads(response.choices[0].message.content)
    result["stem"] = sheet["stem"]
    result["single_sheet_pdf"] = sheet["single_sheet_pdf"]
    result["extraction_path"] = sheet["extraction_path"]
    result["image_path"] = sheet.get("image_path")
    return result


def classify_all_sheets(sheet_index: dict) -> dict:
    """Step 3: classify every sheet in sheet_index.json. Returns the sheet_classification.json
    structure (schema per references/output_schemas.md)."""
    classified = [classify_sheet(s) for s in sheet_index["sheets"]]
    return {"classified_at": None, "model": config.CHEAP_MODEL, "sheets": classified}


def summarize(classification: dict) -> dict:
    """The 'quick summary table' SKILL.md Step 3 asks to show the user: counts per type,
    counts per discipline, low-confidence sheets to flag for review."""
    by_type: dict[str, int] = {}
    by_discipline: dict[str, int] = {}
    low_confidence = []
    for s in classification["sheets"]:
        by_type[s["drawing_type"]] = by_type.get(s["drawing_type"], 0) + 1
        by_discipline[s["discipline"]] = by_discipline.get(s["discipline"], 0) + 1
        if s.get("confidence") == "low":
            low_confidence.append({"stem": s["stem"], "drawing_type": s["drawing_type"], "justification": s.get("justification")})
    return {"by_type": by_type, "by_discipline": by_discipline, "low_confidence": low_confidence}


# ---------------------------------------------------------------------------
# Step 3b -- pre-flight cost gate & scope. "Never run the per-sheet loop on a
# large set without it." Below config.SCOPE_GATE_SHEET_THRESHOLD, scope (a)
# runs automatically -- that's the skill's own stated default for small sets.
# ---------------------------------------------------------------------------
SCOPE_OPTIONS = [
    {"key": "a", "label": "Full set", "description": "Every sheet, exhaustive analysis + database. The default."},
    {
        "key": "b",
        "label": "In-scope discipline only",
        "description": (
            "Exhaustive on the sheets matching the chosen perspective; the rest still listed in the "
            "register and database schedules, but no per-sheet prose."
        ),
    },
    {
        "key": "c",
        "label": "High-leverage subset",
        "description": "You name the specific sheets that matter; only those get exhaustive analysis.",
    },
    {
        "key": "d",
        "label": "Index-light",
        "description": (
            "Register + symbol library + database schedules/instances only, skipping the exhaustive "
            "per-sheet prose. Add prose for a specific sheet later on demand."
        ),
    },
]


def estimate_calls(classification: dict) -> int:
    """Rough relative cost estimate: vision-routed sheets cost far more than text-routed
    ones (image tokens), so weight them rather than just counting sheets. Not a dollar
    figure -- SKILL.md calls for 'a rough token/time estimate ... labelled an estimate'."""
    calls = 0
    for s in classification["sheets"]:
        calls += 3 if route_for(s["drawing_type"]) == "vision" else 1
    return calls


def needs_scope_gate(sheet_count: int) -> bool:
    return sheet_count >= config.SCOPE_GATE_SHEET_THRESHOLD


def resolve_scope(classification: dict, scope: str, perspective: str, named_sheets: list[str] | None = None) -> dict[str, str]:
    """Returns {stem: "exhaustive" | "registered_only"} for the four Step 3b options.
    'registered_only' sheets still get schedules/instances (Steps 5b/5d) -- only the long
    per-sheet markdown prose (Step 5) is skipped for them."""
    sheets = classification["sheets"]
    if scope == "a":
        return {s["stem"]: "exhaustive" for s in sheets}
    if scope == "b":
        return {
            s["stem"]: "exhaustive" if s["discipline"].lower() == perspective.lower() else "registered_only"
            for s in sheets
        }
    if scope == "c":
        named = set(named_sheets or [])
        return {
            s["stem"]: "exhaustive" if (s["stem"] in named or s.get("sheet_id") in named) else "registered_only"
            for s in sheets
        }
    if scope == "d":
        return {s["stem"]: "registered_only" for s in sheets}
    raise ValueError(f"unknown scope {scope!r}")
