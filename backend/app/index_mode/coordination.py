# Vendor scripts used: none -- pure AI judgement (Step 7 coordination pass)
import json

from openai import OpenAI

from .. import config

client = OpenAI(api_key=config.OPENAI_API_KEY)

# references/drawing_types.md's "Coordination pass prompt".
COORDINATION_PROMPT = """You have access to:
- The drawing register (every sheet: ID, title, type, discipline)
- cross_references.json -- every cross-reference detected, with a resolved flag
- symbol_library.json -- every symbol defined in any legend

Surface coordination issues:
1. Cross-references where resolved is false -- list the source sheet and what it points at.
2. Symbols used on a sheet's markdown that don't appear in the symbol library for their
   discipline.
3. Drawing register inconsistencies -- sheets referenced but not present, sheet count
   mismatches, missing revisions.
4. Trade coordination gaps inferred by comparing sheets across disciplines -- e.g.
   "electrical penetrations called out on one sheet with no matching penetration on the
   structural penetration plan." Be specific; cite the sheets you're comparing.

Do not invent issues -- if there is no evidence for something, do not list it. Return
strictly valid markdown (not JSON), grouped by the four categories above, starting with a
one-paragraph summary."""


def run_coordination_pass(sheet_register: list[dict], cross_references: dict, symbol_library: dict) -> str:
    """Step 7: one consolidated LLM call (strong model) -- reads the register + Step 6's
    cross-ref graph + Step 4's symbol library, surfaces coordination issues as markdown."""
    payload = {
        "drawing_register": sheet_register,
        "cross_references": cross_references,
        "symbol_library": symbol_library,
    }
    response = client.chat.completions.create(
        model=config.STRONG_MODEL,
        messages=[
            {"role": "system", "content": COORDINATION_PROMPT},
            {"role": "user", "content": json.dumps(payload)},
        ],
    )
    return response.choices[0].message.content or "# Coordination Issues\n\nNone identified."
