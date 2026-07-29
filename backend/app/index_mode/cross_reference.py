# Vendor scripts used: none -- pure AI judgement (Step 6 cross-reference graph)
import json
import re

from openai import OpenAI

from .. import config

client = OpenAI(api_key=config.OPENAI_API_KEY)

# references/drawing_types.md's "Cross-reference detection prompt" (Claude detects the
# convention -- "don't assume '1/A-301' means anything specific; different offices and
# countries use different conventions").
CROSSREF_PROMPT = """You are building a cross-reference graph for a construction drawing set.

Below is the "Cross-references on this sheet" section from every per-sheet markdown produced
for this project, in the order they were generated.

Your job:
1. Detect the cross-reference CONVENTION used in this set (e.g. "detail-on-top": "1/A-301" =
   detail 1 on sheet A-301; "sheet-on-top": "A-301/1"; letter-based; or a mix for different
   reference types). State what you observe.
2. Build a graph of every cross-reference: source_sheet, source_type
   (section_marker|detail_bubble|schedule_ref|spec_ref), source_label, target_sheet,
   target_label, target_type, and resolved (true if the target sheet is in this set, false
   if not, with a one-line reason).

Return strictly valid JSON, no commentary:
{"convention_detected": "...", "references": [{"source_sheet": "...", "source_type": "...",
 "source_label": "...", "target_sheet": "...", "target_label": "...", "target_type": "...",
 "resolved": true, "reason": "<only if resolved is false>"}]}"""


def extract_crossref_section(markdown: str) -> str:
    """Pull just the '## Cross-references on this sheet' section out of a sheet's full
    markdown -- pure text slicing, keeps the Step 6 prompt from repeating whole sheets."""
    match = re.search(
        r"## Cross-references on this sheet(.*?)(?=\n## |\Z)", markdown, re.DOTALL | re.IGNORECASE
    )
    return match.group(1).strip() if match else ""


def build_cross_references(sheet_markdowns: dict[str, str]) -> dict:
    """Step 6: one consolidated LLM call over every sheet's cross-reference section."""
    sections = {
        sheet_id: extract_crossref_section(md) for sheet_id, md in sheet_markdowns.items() if extract_crossref_section(md)
    }
    if not sections:
        return {"convention_detected": "No cross-references found in this set.", "references": []}

    response = client.chat.completions.create(
        model=config.STRONG_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": CROSSREF_PROMPT},
            {"role": "user", "content": json.dumps(sections)},
        ],
    )
    return json.loads(response.choices[0].message.content)
