import { useState } from "react";
import { askQuestion } from "./api";

export default function ChatPanel({ docId }) {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [asking, setAsking] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    const question = input.trim();
    if (!question || !docId || asking) return;

    // Prior turns, sent so the backend has memory of its own previous questions/answers
    // (e.g. so a reply like "i don't know" lands in context instead of as a fresh question).
    const history = messages
      .filter((m) => !m.isError)
      .map(({ role, content }) => ({ role, content }));

    setMessages((m) => [...m, { role: "user", content: question }]);
    setInput("");
    setAsking(true);

    try {
      const data = await askQuestion(docId, question, history);
      setMessages((m) => [...m, { role: "assistant", content: data.answer, toolCalls: data.tool_calls }]);
    } catch (err) {
      setMessages((m) => [...m, { role: "assistant", content: `Error: ${err.message}`, isError: true }]);
    } finally {
      setAsking(false);
    }
  }

  return (
    <div className="panel chat-panel">
      <h2>2. Ask questions</h2>

      <div className="messages">
        {!docId && <p className="hint">Upload a drawing set first.</p>}
        {messages.map((m, i) => (
          <div key={i} className={`message ${m.role}${m.isError ? " error" : ""}`}>
            <div className="bubble">{m.content}</div>
            {m.toolCalls?.length > 0 && <div className="tool-badges">used: {m.toolCalls.join(", ")}</div>}
          </div>
        ))}
        {asking && <div className="message assistant"><div className="bubble">Thinking…</div></div>}
      </div>

      <form onSubmit={handleSubmit} className="chat-input">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={docId ? "e.g. How many F12 footings are there?" : "Upload a PDF first"}
          disabled={!docId || asking}
        />
        <button type="submit" disabled={!docId || asking || !input.trim()}>
          Ask
        </button>
      </form>
    </div>
  );
}
