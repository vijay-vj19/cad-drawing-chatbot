import { useState } from "react";
import UploadPanel from "./UploadPanel";
import ChatPanel from "./ChatPanel";
import "./App.css";

function App() {
  const [docId, setDocId] = useState(null);

  return (
    <div className="app">
      <h1>Drawing Set Chatbot</h1>
      <div className="layout">
        <UploadPanel onUploaded={setDocId} />
        <ChatPanel docId={docId} />
      </div>
    </div>
  );
}

export default App
