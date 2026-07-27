import { useState } from "react";
import { uploadPdf } from "./api";

export default function UploadPanel({ onUploaded }) {
  const [status, setStatus] = useState("idle"); // idle | uploading | error | done
  const [error, setError] = useState("");
  const [summary, setSummary] = useState(null);

  async function handleFileChange(e) {
    const file = e.target.files?.[0];
    if (!file) return;

    setStatus("uploading");
    setError("");
    setSummary(null);

    try {
      const data = await uploadPdf(file);
      setSummary(data);
      setStatus("done");
      onUploaded(data.doc_id);
    } catch (err) {
      setError(err.message);
      setStatus("error");
    }
  }

  return (
    <div className="panel upload-panel">
      <h2>1. Upload drawing set</h2>
      <input type="file" accept="application/pdf" onChange={handleFileChange} disabled={status === "uploading"} />

      {status === "uploading" && (
        <p className="hint">Ingesting PDF — classifying sheets, extracting data (plan/section sheets are read visually, this can take a bit)…</p>
      )}
      {status === "error" && <p className="error">{error}</p>}

      {status === "done" && summary && (
        <div className="summary">
          <p>
            <strong>{summary.sheet_count}</strong> sheets ingested — {summary.schedule_count} schedule rows,{" "}
            {summary.instance_count} placed instances, {summary.note_count} notes.
            {summary.provenance_corrections > 0 && (
              <> {summary.provenance_corrections} provenance correction{summary.provenance_corrections === 1 ? "" : "s"} applied.</>
            )}
          </p>
          {summary.sheets_failed > 0 && (
            <p className="error">
              {summary.sheets_failed} sheet{summary.sheets_failed === 1 ? "" : "s"} failed to process and may be missing data — try re-uploading.
            </p>
          )}
          <ul>
            {summary.sheets.map((s) => (
              <li key={s.number}>
                <strong>{s.number}</strong> — {s.title || "untitled"} ({s.discipline || "?"}, {s.scale || "no scale detected"})
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
