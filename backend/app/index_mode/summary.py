# Vendor scripts used: none -- pure AI judgement + deterministic formatting (Step 8/9)
import json
from datetime import datetime, timezone

from openai import OpenAI

from .. import config

client = OpenAI(api_key=config.OPENAI_API_KEY)

WHATS_IN_THIS_SET_PROMPT = """Given this drawing set's sheet register (types, disciplines, titles), write ONE
paragraph describing what this set actually is -- the building/project in plain language, the
disciplines present, and any key schedules or standout sheets. Plain prose, no headings, no
bullet points, 3-5 sentences."""


def _whats_in_this_set(sheet_register: list[dict]) -> str:
    response = client.chat.completions.create(
        model=config.CHEAP_MODEL,
        messages=[
            {"role": "system", "content": WHATS_IN_THIS_SET_PROMPT},
            {"role": "user", "content": json.dumps(sheet_register)},
        ],
    )
    return response.choices[0].message.content or ""


def build_drawings_md(
    perspective: str,
    scope_chosen: str,
    sheet_register: list[dict],
    crossref: dict,
    vision_only_sheets: list[str],
) -> str:
    """Step 8: the combined index -- header, what's-in-this-set summary, cross-ref summary,
    drawing register table, discipline one-liners, and a pointer to query mode."""
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"# Drawing Set Index",
        "",
        f"Perspective: {perspective} | Scope: {scope_chosen} | Generated: {date}",
        "",
        "## What's in this set",
        "",
        _whats_in_this_set(sheet_register),
        "",
        "## Cross-references",
        "",
    ]
    refs = crossref.get("references", [])
    resolved = sum(1 for r in refs if r.get("resolved"))
    lines.append(f"{resolved}/{len(refs)} resolved. {crossref.get('convention_detected', '')}")
    if vision_only_sheets:
        lines += ["", "## Raster / no-text sheets (vision-only, flagged as placeholders)", ""]
        lines += [f"- {s}" for s in vision_only_sheets]

    lines += ["", "## Drawing register", "", "Sheet ID | Title | Type | Discipline", "---|---|---|---"]
    for s in sheet_register:
        lines.append(f"{s.get('sheet_id') or s.get('stem')} | {s.get('title') or ''} | {s.get('drawing_type')} | {s.get('discipline')}")

    by_discipline: dict[str, list[str]] = {}
    for s in sheet_register:
        by_discipline.setdefault(s.get("discipline") or "Unknown", []).append(s.get("sheet_id") or s.get("stem"))
    lines += ["", "## By discipline", ""]
    for discipline, sheets in by_discipline.items():
        lines.append(f"- **{discipline}**: {len(sheets)} sheet(s) -- {', '.join(sheets)}")

    lines += [
        "",
        "## Querying this drawing set",
        "",
        "Ask questions through /chat. Counts, locations, and relationships come from the "
        "database (db/project.sqlite); geometry not already captured (areas, annotated "
        "dimensions) is queried from the source PDFs on demand; general/spec questions are "
        "answered from the concept wiki and per-sheet markdown.",
    ]
    return "\n".join(lines)


def count_coordination_issues(coordination_md: str) -> int:
    """Rough count for the Step 9 final summary -- coordination.py returns markdown, not
    structured rows, so count bullet lines as a proxy for 'issues surfaced'."""
    return sum(1 for line in coordination_md.splitlines() if line.strip().startswith("- "))
