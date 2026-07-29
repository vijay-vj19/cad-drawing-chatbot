# Vendor scripts used: none -- pure AI judgement (Step 5f concept wiki)
import json
import re
from pathlib import Path

from openai import OpenAI

from .. import config

client = OpenAI(api_key=config.OPENAI_API_KEY)

CONCEPT_WIKI_PROMPT = """You are building a "concept wiki" from every note/spec/requirement gathered across an
entire construction drawing set. Regroup them BY CONCEPT (not by sheet) -- e.g. all mentions
of concrete grade, wherever they appeared, become one page; same for reinforcement, fire
rating, fixings, testing/commissioning, whatever concepts this specific set actually
warrants. Don't force a fixed list of pages -- infer which concepts this set needs from what
the notes actually cover.

Rules:
- Don't invent anything -- every fact must come from the notes given, cited to its source
  sheet.
- Cross-link related pages with [[wikilinks]] (e.g. a footings page might link
  [[concrete-grades]]).
- If two notes state CONTRADICTORY values for the same thing (e.g. one sheet says 25 MPa,
  another says 32 MPa for what looks like the same element), do not silently pick one --
  flag it as a conflict.
- If a note looks stale/superseded by a later one, flag that too.

Return strictly valid JSON, no commentary:
{"pages": [{"concept": "<kebab-case-slug>", "content": "<full markdown page, citing source
  sheets, using [[wikilinks]] to other pages>"}],
 "conflicts": [{"topic": "...", "description": "...", "sources": ["sheet A", "sheet B"]}]}"""


def build_concept_wiki(all_notes: list[dict]) -> dict:
    """Step 5f: one consolidated LLM call (strong model) over every note gathered in Step 5,
    regrouped by concept instead of by sheet."""
    if not all_notes:
        return {"pages": [], "conflicts": []}

    response = client.chat.completions.create(
        model=config.STRONG_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": CONCEPT_WIKI_PROMPT},
            {"role": "user", "content": json.dumps({"notes": all_notes})},
        ],
    )
    return json.loads(response.choices[0].message.content)


def _safe_slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-") or "page"


def write_concept_wiki(wiki_dir: Path, wiki: dict) -> None:
    """Writes one .md per concept page plus index.md with the conflict/RFI register --
    matching SKILL.md's concept_wiki/ output folder."""
    wiki_dir.mkdir(parents=True, exist_ok=True)
    page_names = []
    for page in wiki.get("pages", []):
        slug = _safe_slug(page.get("concept", "page"))
        (wiki_dir / f"{slug}.md").write_text(page.get("content", ""), encoding="utf-8")
        page_names.append(slug)

    index_lines = ["# Concept Wiki Index", "", "## Pages", ""]
    index_lines += [f"- [[{name}]]" for name in page_names]
    index_lines += ["", "## Conflict / RFI register", ""]
    conflicts = wiki.get("conflicts", [])
    if not conflicts:
        index_lines.append("None identified.")
    else:
        for c in conflicts:
            sources = ", ".join(c.get("sources", []))
            index_lines.append(f"- **{c.get('topic')}** -- {c.get('description')} (sources: {sources})")

    (wiki_dir / "index.md").write_text("\n".join(index_lines), encoding="utf-8")
