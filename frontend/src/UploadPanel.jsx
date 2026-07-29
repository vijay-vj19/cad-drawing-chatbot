import { useState } from "react";
import { chooseScope, uploadPdf } from "./api";

// SKILL.md's named trade perspectives -- analysis is tuned to whichever one is chosen.
const PERSPECTIVES = [
  "General Contractor",
  "Electrical",
  "Hydraulic",
  "Mechanical",
  "Structural",
  "Civil",
  "Fire",
  "Communications",
];

export default function UploadPanel({ onUploaded }) {
  const [status, setStatus] = useState("idle"); // idle | uploading | awaiting_scope | choosing_scope | error | done
  const [error, setError] = useState("");
  const [summary, setSummary] = useState(null);
  const [perspective, setPerspective] = useState(PERSPECTIVES[0]);

  async function handleFileChange(e) {
    const file = e.target.files?.[0];
    if (!file) return;

    setStatus("uploading");
    setError("");
    setSummary(null);

    try {
      const data = await uploadPdf(file, perspective);
      setSummary(data);
      if (data.awaiting_scope) {
        setStatus("awaiting_scope");
      } else {
        setStatus("done");
        onUploaded(data.doc_id);
      }
    } catch (err) {
      setError(err.message);
      setStatus("error");
    }
  }

  async function handleScopeChoice(scope) {
    if (!summary) return;
    setStatus("choosing_scope");
    setError("");
    try {
      const data = await chooseScope(summary.doc_id, scope);
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

      <label className="field-label" htmlFor="perspective-input">
        Perspective
      </label>
      <select
        id="perspective-input"
        value={perspective}
        onChange={(e) => setPerspective(e.target.value)}
        disabled={status === "uploading" || status === "choosing_scope"}
      >
        {PERSPECTIVES.map((p) => (
          <option key={p} value={p}>{p}</option>
        ))}
      </select>

      <label className="field-label" htmlFor="pdf-input">
        Drawing set (PDF)
      </label>
      <input
        id="pdf-input"
        type="file"
        accept="application/pdf"
        onChange={handleFileChange}
        disabled={status === "uploading" || status === "choosing_scope"}
      />

      {status === "uploading" && (
        <p className="hint">Splitting, classifying, and analysing the drawing set — this can take a bit for plan/section sheets.</p>
      )}
      {status === "error" && <p className="error">{error}</p>}

      {status === "awaiting_scope" && summary && (
        <div className="summary">
          <p>
            <strong>{summary.sheet_count}</strong> sheets found ({summary.vision_only_count} raster/no-text). This is a large
            set — pick how much to analyse (rough relative cost estimate: {summary.estimated_calls}):
          </p>
          <ul>
            {summary.by_type.map((t) => (
              <li key={t.label}>{t.label}: {t.count}</li>
            ))}
          </ul>
          {summary.scope_options.map((opt) => (
            <div key={opt.key} className="scope-option">
              <button onClick={() => handleScopeChoice(opt.key)}>{opt.label}</button>
              <span>{opt.description}</span>
            </div>
          ))}
        </div>
      )}

      {status === "choosing_scope" && <p className="hint">Running the full analysis for the chosen scope…</p>}

      {status === "done" && summary && summary.mode === "light" && (
        <div className="summary">
          <p>
            <strong>{summary.sheet_count}</strong> sheet(s) — read directly, no database built.
          </p>
          <p className="hint">Why: {summary.reason}</p>
          <ul>
            {summary.sheets.map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ul>
        </div>
      )}

      {status === "done" && summary && summary.mode !== "light" && !summary.awaiting_scope && (
        <div className="summary">
          <p>
            <strong>{summary.sheet_count}</strong> sheets analysed ({summary.sheets_analysed} in full, {summary.sheets_registered_only} registered only) —{" "}
            {summary.table_counts.schedules} schedule rows, {summary.table_counts.instances} placed instances,{" "}
            {summary.table_counts.notes} notes, {summary.symbol_count} symbols, {summary.coordination_issue_count} coordination issue(s) flagged.
            {summary.provenance_corrections > 0 && (
              <> {summary.provenance_corrections} provenance correction{summary.provenance_corrections === 1 ? "" : "s"} applied.</>
            )}
          </p>
          {summary.vision_only_sheets.length > 0 && (
            <p className="hint">Raster/no-text sheets (vision-only): {summary.vision_only_sheets.join(", ")}</p>
          )}
          <ul>
            {summary.by_type.map((t) => (
              <li key={t.label}>{t.label}: {t.count}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
