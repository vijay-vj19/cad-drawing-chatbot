import shutil
import uuid

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from . import chat, config, ingest
from .models import ChatRequest, ChatResponse, UploadResponse

app = FastAPI(title="Construction Drawing RAG Chatbot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/upload", response_model=UploadResponse)
async def upload(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Please upload a PDF file.")

    doc_id = uuid.uuid4().hex[:12]
    dest = config.doc_dir(doc_id) / "original.pdf"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        summary = ingest.ingest_pdf(doc_id, dest)
    except ingest.GateRejected as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {e}")

    return summary


@app.post("/chat", response_model=ChatResponse)
async def ask(req: ChatRequest):
    doc_dir = config.DATA_DIR / req.doc_id
    if not doc_dir.exists():
        raise HTTPException(status_code=404, detail="Unknown doc_id -- upload a PDF first.")

    history = [m.model_dump() for m in req.history]
    result = chat.answer_question(req.doc_id, req.question, history)
    return result


@app.get("/health")
async def health():
    return {"status": "ok"}
