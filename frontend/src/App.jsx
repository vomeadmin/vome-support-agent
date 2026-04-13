import { useState } from "react";
import { setToken } from "./api";
import Dashboard from "./components/Dashboard";

export default function App() {
  const [authed, setAuthed] = useState(
    () => !!localStorage.getItem("ops_token")
  );
  const [tokenInput, setTokenInput] = useState("");

  if (!authed) {
    return (
      <div className="login-screen">
        <div className="login-box">
          <h1>Vome Command Center</h1>
          <p>Enter your ops token to continue.</p>
          <input
            type="password"
            value={tokenInput}
            onChange={(e) => setTokenInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && tokenInput.trim()) {
                setToken(tokenInput.trim());
                setAuthed(true);
              }
            }}
            placeholder="OPS_TOKEN"
            autoFocus
          />
          <button
            onClick={() => {
              if (tokenInput.trim()) {
                setToken(tokenInput.trim());
                setAuthed(true);
              }
            }}
          >
            Sign in
          </button>
        </div>
      </div>
    );
  }

  return <Dashboard />;
}
