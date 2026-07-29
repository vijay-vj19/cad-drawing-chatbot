import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
VENDOR_DIR = BASE_DIR / "vendor" / "drawings_analyser"
SCRIPTS_DIR = VENDOR_DIR / "scripts"
REFERENCES_DIR = VENDOR_DIR / "references"
DATA_DIR = BASE_DIR / "data"

try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env", override=True)
except ImportError:
    pass

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# SKILL.md "Route on judgement load": text-dominant sheets + light classification go to
# the cheap model; spatial/symbol/connectivity judgement (plans, sections, gestalt,
# coordination, concept wiki) goes to the strong model. See the ROUTING table in
# index_mode/classify.py, which mirrors SKILL.md's own routing table verbatim.
STRONG_MODEL = os.environ.get("STRONG_MODEL", "gpt-4o")
CHEAP_MODEL = os.environ.get("CHEAP_MODEL", "gpt-4o-mini")

# SKILL.md learning #5 (the vector/raster fork): below this many extracted characters per
# page, treat the sheet as having no usable text layer (scanned/outlined, not CAD-plotted).
TEXT_LAYER_MIN_CHARS_PER_PAGE = 200

# SKILL.md Step 3b: "Default to (a) [full set] only for small sets (roughly < 25 sheets).
# For larger sets, present the options and wait." This is that threshold.
SCOPE_GATE_SHEET_THRESHOLD = 25

# process_drawing.py's own default (2576px) is tuned for Claude's native vision resolution --
# we call OpenAI's vision API instead, which downscales close to this range server-side
# anyway (no quality loss), so a smaller render meaningfully cuts memory/payload size on a
# memory-constrained host with no accuracy cost.
RENDER_LONG_EDGE_PX = int(os.environ.get("RENDER_LONG_EDGE_PX", "1600"))

DATA_DIR.mkdir(parents=True, exist_ok=True)


def doc_dir(doc_id: str) -> Path:
    d = DATA_DIR / doc_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_dir(doc_id: str) -> Path:
    d = doc_dir(doc_id) / "db"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sqlite_path(doc_id: str) -> Path:
    return db_dir(doc_id) / "project.sqlite"
