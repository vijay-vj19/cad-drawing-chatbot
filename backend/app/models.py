from typing import Optional

from pydantic import BaseModel


class SheetSummary(BaseModel):
    number: str
    title: Optional[str] = None
    discipline: Optional[str] = None
    scale: Optional[str] = None


class UploadResponse(BaseModel):
    doc_id: str
    sheet_count: int
    sheets: list[SheetSummary]
    schedule_count: int
    instance_count: int
    note_count: int
    provenance_corrections: int = 0


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    doc_id: str
    question: str
    history: list[ChatMessage] = []


class ChatResponse(BaseModel):
    answer: str
    tool_calls: list[str] = []
