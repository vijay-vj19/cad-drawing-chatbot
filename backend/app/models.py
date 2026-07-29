from pydantic import BaseModel


class SheetBreakdownEntry(BaseModel):
    label: str
    count: int


class ScopeOption(BaseModel):
    key: str  # "a" | "b" | "c" | "d"
    label: str
    description: str


class AwaitingScopeResponse(BaseModel):
    doc_id: str
    awaiting_scope: bool = True
    sheet_count: int
    by_type: list[SheetBreakdownEntry]
    by_discipline: list[SheetBreakdownEntry]
    vision_only_count: int
    estimated_calls: int
    scope_options: list[ScopeOption]


class LightModeSummary(BaseModel):
    doc_id: str
    mode: str = "light"
    reason: str
    sheet_count: int
    sheets: list[str]


class UploadSummary(BaseModel):
    doc_id: str
    mode: str = "full"
    awaiting_scope: bool = False
    perspective: str
    sheet_count: int
    by_type: list[SheetBreakdownEntry]
    by_discipline: list[SheetBreakdownEntry]
    scope_chosen: str
    sheets_analysed: int
    sheets_registered_only: int
    models_used: dict[str, str]
    symbol_count: int
    crossref_resolved: int
    crossref_unresolved: int
    coordination_issue_count: int
    table_counts: dict[str, int]
    provenance_corrections: int
    context_note_count: int
    vision_only_sheets: list[str]
    parse_failures: list[str]


class ScopeChoice(BaseModel):
    scope: str  # "a" | "b" | "c" | "d"
    sheets: list[str] = []  # only used for scope "c" (named high-leverage subset)


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
