const API_BASE = import.meta.env.VITE_API_BASE || "http://127.0.0.1:8000";

export async function uploadPdf(file, perspective) {
  const form = new FormData();
  form.append("file", file);
  if (perspective) form.append("perspective", perspective);

  const res = await fetch(`${API_BASE}/upload`, { method: "POST", body: form });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || `Upload failed (${res.status})`);
  }
  return data;
}

export async function chooseScope(docId, scope, sheets = []) {
  const res = await fetch(`${API_BASE}/upload/${docId}/scope`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope, sheets }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || `Scope choice failed (${res.status})`);
  }
  return data;
}

export async function askQuestion(docId, question, history = []) {
  const res = await fetch(`${API_BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ doc_id: docId, question, history }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || `Request failed (${res.status})`);
  }
  return data;
}
