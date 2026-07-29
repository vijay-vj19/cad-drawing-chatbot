import shutil
import uuid

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from . import config
from .index_mode import orchestrator
from .models import AwaitingScopeResponse, ChatRequest, ChatResponse, ScopeChoice, UploadSummary
from .query_mode.chat import answer_question

app = FastAPI(title="Construction Drawing RAG Chatbot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/upload")
async def upload(file: UploadFile = File(...), perspective: str = Form("general contractor")):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Please upload a PDF file.")

    doc_id = uuid.uuid4().hex[:12]
    dest = config.doc_dir(doc_id) / "original.pdf"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        result = orchestrator.start_ingest(doc_id, dest, perspective)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {e}")

    return result


@app.post("/upload/{doc_id}/scope")
async def choose_scope(doc_id: str, choice: ScopeChoice):
    try:
        return orchestrator.resume_ingest(doc_id, choice.scope, choice.sheets)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="No pending upload awaiting a scope choice for this doc_id.")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {e}")


@app.post("/chat", response_model=ChatResponse)
async def ask(req: ChatRequest):
    doc_dir = config.DATA_DIR / req.doc_id
    if not doc_dir.exists():
        raise HTTPException(status_code=404, detail="Unknown doc_id -- upload a PDF first.")

    history = [m.model_dump() for m in req.history]
    result = answer_question(req.doc_id, req.question, history)
    return result


@app.get("/health")
async def health():
    return {"status": "ok"}
