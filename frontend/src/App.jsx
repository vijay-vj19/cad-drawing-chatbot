import { useState } from "react";
import UploadPanel from "./UploadPanel";
import ChatPanel from "./ChatPanel";
import "./App.css";

function App() {
  const [docId, setDocId] = useState(null);

  return (
    <div className="app">
      <div className="app-header">
        <h1>Drawing Set Chatbot</h1>
        <p className="app-subtitle">Upload a CAD drawing set, then ask questions about it.</p>
      </div>
      <div className="layout">
        <UploadPanel onUploaded={setDocId} />
        <ChatPanel docId={docId} />
      </div>
    </div>
  );
}

export default App
