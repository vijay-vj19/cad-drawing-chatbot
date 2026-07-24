# CAD Drawing Chatbot

A chatbot that answers questions about construction drawing PDFs. Upload a CAD-plotted PDF (must have a real text layer, not a scan) and ask things like "how many F12 footings are there" or "what's the fire rating on door D06" — counts and locations come from data extracted out of the PDF's vector text, not from a model looking at an image.

## How it works

1. **Upload** — the PDF is gated on having a real text layer, then split into per-sheet PDFs with every word's exact (x, y) position extracted (no AI, just [PyMuPDF](https://pymupdf.readthedocs.io/)/[pdfplumber](https://github.com/jsvine/pdfplumber), via the vendored `drawings-analyser` skill's scripts in `backend/vendor/drawings_analyser/`).
2. **Understand** — one GPT-4o call reads all the sheets' text and returns sheet metadata, parsed schedules, notes, and the regex tag pattern for each schedule (e.g. footing marks look like `F\d{2}`).
3. **Count** — those tag patterns are regex-matched against the real word positions (deterministic, no AI) to produce exact instance counts with coordinates.
4. **Store** — everything lands in one SQLite file per document (`sheets`, `schedules`, `instances`, `notes`), plus per-sheet markdown summaries embedded into a `chunks` table for semantic search.
5. **Chat** — GPT-4o answers each question using three tools: SQL over the structured tables (counts/locations), semantic search over the markdown (specs/notes), and on-demand PDF queries (areas/dimensions not already captured). Every answer cites its source sheet and a reliability level (HIGH = read directly from text, MEDIUM = derived/scaled).

## Prerequisites

- Python 3.11+
- Node.js 18+
- An [OpenAI API key](https://platform.openai.com/api-keys) with access to `gpt-4o` and `text-embedding-3-small`

## Setup

Clone the repo, then set up the backend and frontend in separate terminals.

### Backend

```bash
cd backend
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env
# then edit .env and set OPENAI_API_KEY=sk-...
```

Run it:

```bash
uvicorn app.main:app --reload
```

The API is now at `http://127.0.0.1:8000` (see `/docs` for the auto-generated OpenAPI page).

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open the URL Vite prints (default `http://localhost:5173`). It talks to the backend at `http://127.0.0.1:8000` by default — override with a `.env` (see `frontend/.env.example`) if your backend runs elsewhere.

## Using it

1. Upload a CAD-plotted PDF in the left panel. Ingestion runs a live GPT-4o call, so it takes a few seconds; you'll see a summary of sheets/schedules/instances/notes found once it's done.
2. Ask questions in the right panel. The chat remembers the last few turns, so follow-ups work.
3. A PDF with no real text layer (e.g. a scanned image) is rejected at upload with a clear error — there's no OCR fallback.

## Project layout

```
backend/
  vendor/drawings_analyser/   vendored skill: deterministic PDF-parsing scripts + reference docs
  app/
    main.py       FastAPI routes: /upload, /chat, /health
    ingest.py     upload pipeline: gate -> split/extract -> GPT-4o structured extraction -> instance counting -> SQLite + embeddings
    chat.py       GPT-4o function-calling loop + its three tools (SQL, semantic search, on-demand PDF query)
    db.py         SQLite helpers + cosine-similarity search over the embedded chunks
    config.py     environment/settings
    models.py     request/response schemas
frontend/
  src/
    UploadPanel.jsx   PDF upload
    ChatPanel.jsx     chat UI, keeps conversation history
    api.js            fetch wrappers
```

## Notes

- Everything runs locally except the OpenAI API calls — no other external services, no Docker, no separate database server. Each uploaded document gets its own SQLite file under `backend/data/<doc_id>/` (gitignored — this is per-user runtime data, not source).
- The vector search is a plain SQLite table with embeddings compared by cosine similarity in Python — fine at the scale of a handful of drawing sheets, not meant to scale past that.
