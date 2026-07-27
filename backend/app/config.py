import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
VENDOR_DIR = BASE_DIR / "vendor" / "drawings_analyser"
SCRIPTS_DIR = VENDOR_DIR / "scripts"
DATA_DIR = BASE_DIR / "data"

# override=True so backend/.env wins over a stale OPENAI_API_KEY already sitting in the
# shell's environment (e.g. an old/quota-exceeded key exported in a parent shell).
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env", override=True)
except ImportError:
    pass

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "gpt-4o")
# Cheap/fast model for sheet classification and text-only sheet extraction (notes/schedule/
# legend/cover sheets -- the skill's own routing table sends these to its cheapest tier
# since the vector text layer is already 100% accurate for them; no vision needed).
CLASSIFY_MODEL = os.environ.get("CLASSIFY_MODEL", "gpt-4o-mini")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")

# Below this many characters of extracted text per page, treat the sheet as having
# no usable text layer (scanned/outlined, not CAD-plotted vector text).
TEXT_LAYER_MIN_CHARS_PER_PAGE = 200

DATA_DIR.mkdir(parents=True, exist_ok=True)


def doc_dir(doc_id: str) -> Path:
    d = DATA_DIR / doc_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def sqlite_path(doc_id: str) -> Path:
    return doc_dir(doc_id) / "project.sqlite"
