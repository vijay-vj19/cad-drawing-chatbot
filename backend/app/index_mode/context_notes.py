# Vendor scripts used: none -- pure AI judgement (Step 5c gestalt vision pass)
import base64
import json
from pathlib import Path

from openai import OpenAI

from .. import config

client = OpenAI(api_key=config.OPENAI_API_KEY)

# SKILL.md Step 5c: "Run vision for gestalt ONLY -- never invent counts or precise
# dimensions." Only runs on plan sheets, where extents/runs/zoning are visible at all.
CONTEXT_NOTES_PROMPT = """You are looking at ONE rendered plan sheet from a construction drawing set, taking a
qualitative, big-picture pass over it -- extents, runs, significance, zoning. Examples of the
kind of thing to capture: "the main concrete footprint is the warehouse slab, spanning ~the
full grid"; "the west-wall waste main runs ~the full building length"; "bracing concentrates
at the building ends".

This is NOT a counting or measuring pass -- never state a precise count or dimension here
(that belongs in the structured extraction, from text). Only qualitative observations a
person would notice looking at the whole sheet at once.

If nothing notable stands out beyond what a plain description would already say, return an
empty list -- don't manufacture an observation.

Return strictly valid JSON, no commentary: {"notes": [{"note": "...", "basis": "<what in the
image supports this>", "what_to_verify": "<a specific, concrete thing to check to confirm
this>"}]}"""


def extract_context_notes(sheet: dict) -> list[dict]:
    """Step 5c: one vision call per plan sheet (strong model). Always MEDIUM reliability --
    this is a visual judgement call, never confirmed against exact text."""
    if not sheet.get("image_path") or not Path(sheet["image_path"]).exists():
        return []

    b64 = base64.b64encode(Path(sheet["image_path"]).read_bytes()).decode()
    response = client.chat.completions.create(
        model=config.STRONG_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": CONTEXT_NOTES_PROMPT},
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}],
            },
        ],
    )
    result = json.loads(response.choices[0].message.content)
    return [
        {
            "note": n.get("note"),
            "reliability": "MEDIUM",
            "basis": n.get("basis"),
            "what_to_verify": n.get("what_to_verify"),
            "source_sheet": sheet.get("sheet_id") or sheet["stem"],
        }
        for n in result.get("notes", [])
    ]
