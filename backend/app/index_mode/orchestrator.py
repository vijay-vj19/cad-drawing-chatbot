# Vendor scripts used: none directly -- this is the glue that calls every module above in
# order (Steps 2-9); each of those modules names its own vendor script, if any.
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import config, db
from . import classify, concept_wiki, context_notes, coordination, cross_reference, database, instances, provenance, split, summary, symbols, triage
from .analysis import analyze_sheet


def _sheet_label(sheet: dict) -> str:
    return sheet.get("sheet_id") or sheet["stem"]


def start_ingest(doc_id: str, pdf_path: Path, perspective: str) -> dict:
    """Steps 2-3: split + classify. If the set is small (below the Step 3b threshold), goes
    straight on to run the full pipeline. Otherwise pauses and returns the cost estimate,
    saving state so resume_ingest can pick back up once the user chooses a scope."""
    d = config.doc_dir(doc_id)
    drawings_split_root = d / "drawings_split"
    # process_drawing.py's own convention: one subfolder per source PDF, named after its
    # stem -- build_sheet_index.py then walks every subfolder under the parent.
    split_dir = drawings_split_root / pdf_path.stem
    manifest = split.run_process_drawing(pdf_path, split_dir)
    sheet_index = split.build_sheet_index(drawings_split_root, d / "sheet_index.json")

    # The triage decision: build the full structured database, or read this set directly at
    # query time. A genuine per-document judgement call, not a hardcoded sheet-count cutoff.
    mode_decision = triage.decide_mode(sheet_index)
    (d / "mode.json").write_text(json.dumps(mode_decision))
    if mode_decision["mode"] == "light":
        return run_light_ingest(doc_id, sheet_index, mode_decision["reason"])

    # Sequential (max_workers=1), not parallel -- even 2 concurrent sheets (each holding a
    # base64-encoded PNG + request/response payload in memory at once) still OOM-killed this
    # on a 512MB host. Trades wall-clock speed for staying within a small memory budget.
    with ThreadPoolExecutor(max_workers=1) as pool:
        classified_sheets = list(pool.map(classify.classify_sheet, sheet_index["sheets"]))
    classification = {"classified_at": None, "model": config.CHEAP_MODEL, "sheets": classified_sheets}
    (d / "sheet_classification.json").write_text(json.dumps(classification, indent=2))

    sheet_count = len(classification["sheets"])
    if not classify.needs_scope_gate(sheet_count):
        return run_full_pipeline(doc_id, classification, perspective, scope="a")

    (d / "pending_scope.json").write_text(json.dumps({"classification": classification, "perspective": perspective}))
    stats = classify.summarize(classification)
    return {
        "doc_id": doc_id,
        "awaiting_scope": True,
        "sheet_count": sheet_count,
        "by_type": [{"label": k, "count": v} for k, v in stats["by_type"].items()],
        "by_discipline": [{"label": k, "count": v} for k, v in stats["by_discipline"].items()],
        "vision_only_count": sum(1 for s in classification["sheets"] if split.is_raster_sheet(s["extraction_path"])),
        "estimated_calls": classify.estimate_calls(classification),
        "scope_options": classify.SCOPE_OPTIONS,
    }


def run_light_ingest(doc_id: str, sheet_index: dict, reason: str) -> dict:
    """Light mode: no classification, no database, no markdown -- just the Step 2 split
    output (already produced) plus two small lookup files so query-time tools (query_pdf,
    view_sheet_image) know where to find each sheet's PDF/image. Keyed by sheet *stem* since
    there's no classified sheet_id in this mode."""
    d = config.doc_dir(doc_id)
    sheet_files = {s["stem"]: s["single_sheet_pdf"] for s in sheet_index["sheets"]}
    sheet_images = {s["stem"]: s["image_path"] for s in sheet_index["sheets"] if s.get("image_path")}
    (d / "sheet_files.json").write_text(json.dumps(sheet_files, indent=2))
    (d / "sheet_images.json").write_text(json.dumps(sheet_images, indent=2))

    return {
        "doc_id": doc_id,
        "mode": "light",
        "reason": reason,
        "sheet_count": len(sheet_index["sheets"]),
        "sheets": [s["stem"] for s in sheet_index["sheets"]],
    }


def resume_ingest(doc_id: str, scope: str, named_sheets: list[str]) -> dict:
    """Step 3b's resume path: the user has now picked a scope for a large set."""
    d = config.doc_dir(doc_id)
    pending = json.loads((d / "pending_scope.json").read_text())
    return run_full_pipeline(doc_id, pending["classification"], pending["perspective"], scope, named_sheets)


def run_full_pipeline(doc_id: str, classification: dict, perspective: str, scope: str, named_sheets: list[str] | None = None) -> dict:
    """Steps 4-9: everything from the symbol library through the final summary, for
    whichever sheets/scope was chosen (or the automatic scope 'a' default)."""
    d = config.doc_dir(doc_id)
    scope_by_stem = classify.resolve_scope(classification, scope, perspective, named_sheets)
    sheets = classification["sheets"]

    # Step 4: symbol library, from every legend-classified sheet.
    legend_sheets = [s for s in sheets if s["drawing_type"] == "legend"]
    symbol_library = symbols.build_symbol_library(legend_sheets, d / "symbol_crops")
    (d / "symbol_library.json").write_text(json.dumps(symbol_library, indent=2))

    # Step 5 (+ its structured-facts side): one call per sheet, sequential -- see the
    # max_workers note on the classification pool above (same 512MB constraint applies here,
    # and this step's payloads are larger: full vision images + longer per-type prompts).
    with ThreadPoolExecutor(max_workers=1) as pool:
        analyses = list(pool.map(lambda s: analyze_sheet(s, s["drawing_type"], perspective), sheets))

    sheets_rows, schedules_rows, notes_rows, relationships_rows = [], [], [], []
    instances_rows, context_notes_rows = [], []
    sheet_markdowns: dict[str, str] = {}
    sheet_texts: dict[str, str] = {}
    sheet_files: dict[str, str] = {}
    sheet_images: dict[str, str] = {}
    vision_only_sheets: list[str] = []
    markdown_dir = d / "drawings"
    markdown_dir.mkdir(exist_ok=True)

    for sheet, analysis in zip(sheets, analyses):
        label = _sheet_label(sheet)
        sheet_texts[label] = json.loads(Path(sheet["extraction_path"]).read_text()).get("all_text", "")
        sheet_files[label] = sheet["single_sheet_pdf"]
        if sheet.get("image_path"):
            sheet_images[label] = sheet["image_path"]
        meta = analysis.get("sheet", {})
        sheets_rows.append(
            {
                "sheet_id": label,
                "title": meta.get("title") or sheet.get("title"),
                "drawing_type": sheet["drawing_type"],
                "discipline": sheet.get("discipline"),
                "scale": meta.get("scale"),
                "rev": meta.get("rev"),
                "source_pdf": sheet["single_sheet_pdf"],
            }
        )
        for row in analysis.get("schedules", []):
            schedules_rows.append({**row, "source_sheet": label})
        for row in analysis.get("notes", []):
            notes_rows.append({**row, "source_sheet": label})
        for row in analysis.get("relationships", []):
            relationships_rows.append({**row, "source_sheet": label})

        if split.is_raster_sheet(sheet["extraction_path"]):
            vision_only_sheets.append(label)

        markdown = analysis.get("markdown", "")
        sheet_markdowns[label] = markdown
        if scope_by_stem.get(sheet["stem"]) == "exhaustive":
            safe_name = label.replace("/", "_").replace(" ", "_")
            (markdown_dir / f"{safe_name}.md").write_text(markdown, encoding="utf-8")

        # Step 5b: instance extraction, only on plan sheets, only where in-scope work applies.
        instances_rows += instances.extract_all_instances(
            {**sheet, "sheet_id": label}, sheet["drawing_type"], analysis.get("instance_targets", []), d / "drawings_split"
        )

        # Step 5c: gestalt vision pass, plan sheets only, exhaustive-scope sheets only.
        if sheet["drawing_type"] == "general_arrangement" and scope_by_stem.get(sheet["stem"]) == "exhaustive":
            context_notes_rows += context_notes.extract_context_notes({**sheet, "sheet_id": label})

    # Lookups query mode needs to open the right file for a given sheet ID.
    (d / "sheet_files.json").write_text(json.dumps(sheet_files, indent=2))
    (d / "sheet_images.json").write_text(json.dumps(sheet_images, indent=2))

    # Step 5d: assemble + load the database.
    structured = database.assemble_structured(
        sheets_rows, schedules_rows, instances_rows, notes_rows, relationships_rows, context_notes_rows, placeholders=[]
    )
    structured_path = d / "db" / "structured.json"
    structured_path.parent.mkdir(exist_ok=True)
    structured_path.write_text(json.dumps(structured, indent=2))
    database.run_build_db(structured_path, config.sqlite_path(doc_id))

    # Step 5e: provenance validation (mandatory) -- relocate mis-sourced schedule/note rows.
    text_dir = d / "sheet_text"
    provenance.write_sheet_text_files(text_dir, sheet_texts)
    corrections = provenance.run_provenance_validation(structured_path, text_dir, d / "db" / "provenance_report.json")
    if corrections:
        validated_path = provenance.validated_structured_path(structured_path)
        if validated_path.exists():
            database.run_build_db(validated_path, config.sqlite_path(doc_id))

    conn = db.get_conn(doc_id)
    db.ensure_schema(conn)
    table_counts = db.table_counts(conn)
    conn.close()

    # Step 5f: concept wiki, over every note gathered across the whole set.
    wiki = concept_wiki.build_concept_wiki(notes_rows)
    concept_wiki.write_concept_wiki(d / "concept_wiki", wiki)

    # Step 6: cross-reference graph, over every sheet's markdown.
    crossref = cross_reference.build_cross_references(sheet_markdowns)
    (d / "cross_references.json").write_text(json.dumps(crossref, indent=2))

    # Step 7: coordination pass.
    sheet_register = [{"sheet_id": _sheet_label(s), "title": s.get("title"), "drawing_type": s["drawing_type"], "discipline": s.get("discipline")} for s in sheets]
    coordination_md = coordination.run_coordination_pass(sheet_register, crossref, symbol_library)
    (d / "coordination_issues.md").write_text(coordination_md, encoding="utf-8")

    # Step 8: combined index.
    drawings_md = summary.build_drawings_md(perspective, scope, sheet_register, crossref, vision_only_sheets)
    (d / "drawings.md").write_text(drawings_md, encoding="utf-8")

    # Step 9: final summary payload.
    stats = classify.summarize(classification)
    refs = crossref.get("references", [])
    return {
        "doc_id": doc_id,
        "mode": "full",
        "awaiting_scope": False,
        "perspective": perspective,
        "sheet_count": len(sheets),
        "by_type": [{"label": k, "count": v} for k, v in stats["by_type"].items()],
        "by_discipline": [{"label": k, "count": v} for k, v in stats["by_discipline"].items()],
        "scope_chosen": scope,
        "sheets_analysed": sum(1 for v in scope_by_stem.values() if v == "exhaustive"),
        "sheets_registered_only": sum(1 for v in scope_by_stem.values() if v == "registered_only"),
        "models_used": {"classification": config.CHEAP_MODEL, "analysis": f"{config.CHEAP_MODEL} (text) / {config.STRONG_MODEL} (vision)"},
        "symbol_count": len(symbol_library.get("symbols", [])),
        "crossref_resolved": sum(1 for r in refs if r.get("resolved")),
        "crossref_unresolved": sum(1 for r in refs if not r.get("resolved")),
        "coordination_issue_count": summary.count_coordination_issues(coordination_md),
        "table_counts": table_counts,
        "provenance_corrections": corrections,
        "context_note_count": len(context_notes_rows),
        "vision_only_sheets": vision_only_sheets,
        "parse_failures": [],
    }
