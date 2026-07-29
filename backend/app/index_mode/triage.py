# Vendor scripts used: none -- pure AI judgement (the mode-decision call)
import json
from pathlib import Path

from openai import OpenAI

from .. import config

client = OpenAI(api_key=config.OPENAI_API_KEY)

TRIAGE_PROMPT = """You are deciding how to handle a construction drawing set that has just been split into
sheets. Two options:

- "full": build a structured database -- classify every sheet, extract schedules/notes/tagged
  instances with coordinates, validate provenance, build a concept wiki, cross-reference
  graph, coordination pass. Worth it for a set large enough, or complex enough, that the same
  facts will get looked up repeatedly and a database pays for itself.
- "light": skip all of that and answer questions by reading the sheets directly at query time
  (their text and, if needed, their rendered images). Better for a small/simple set where
  building a whole database is more overhead than the set warrants.

You're given each sheet's stem, a short text sample, and its character count (a rough proxy
for how text-heavy vs sparse it is). Decide based on the actual set in front of you -- sheet
count matters, but so does how much genuinely structured/repeatable content there is (multiple
schedules, many tagged instances) versus a simple, small set that's easy to just read.

Return strictly valid JSON, no commentary: {"mode": "full" or "light", "reason": "<one
sentence, plain language, explaining the call -- this may be shown to the user>"}"""


def decide_mode(sheet_index: dict) -> dict:
    """The triage decision: build the full structured database, or read this set directly
    at query time. A genuine judgement call, not a hardcoded sheet-count threshold. This call
    is text-only (no images), keeping the decision itself cheap regardless of the outcome."""
    sheets_summary = []
    for s in sheet_index.get("sheets", []):
        extraction = json.loads(Path(s["extraction_path"]).read_text())
        text = extraction.get("all_text", "")
        sheets_summary.append({"stem": s["stem"], "char_count": len(text), "text_sample": text[:300]})

    response = client.chat.completions.create(
        model=config.CHEAP_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": TRIAGE_PROMPT},
            {"role": "user", "content": json.dumps({"sheet_count": len(sheets_summary), "sheets": sheets_summary})},
        ],
    )
    result = json.loads(response.choices[0].message.content)
    if result.get("mode") not in ("full", "light"):
        result["mode"] = "full"
    return result
