import { useState } from "react";

const ZOHO_STATUSES = [
  "On Hold",
  "Processing",
  "Final Review",
  "Closed",
];

const CLICKUP_ACTIONS = [
  { value: "leave", label: "Leave as-is" },
  { value: "close_temporarily", label: "Close temporarily" },
  { value: "in_progress", label: "Move to In Progress" },
  { value: "waiting_on_client", label: "Waiting on Client" },
  { value: "done", label: "Mark Done" },
];

export default function DraftPanel({
  ticket,
  draft,
  draftLoading,
  draftSending,
  draftError,
  onGenerate,
  onSend,
  onDiscard,
  onDraftChange,
}) {
  const [editing, setEditing] = useState(false);
  const [redraftText, setRedraftText] = useState("");
  const [showRedraft, setShowRedraft] = useState(false);
  const [zohoStatus, setZohoStatus] = useState(
    draft?.suggested_zoho_status || "On Hold"
  );
  const [clickupAction, setClickupAction] = useState(
    draft?.suggested_clickup_action || "leave"
  );

  if (!ticket) return null;

  const hasDraft = draft?.draft && !draft.draft.startsWith("(Draft generation failed");

  return (
    <div className="panel-overlay" onClick={onDiscard}>
      <div className="draft-panel" onClick={(e) => e.stopPropagation()}>
        <div className="panel-header">
          <div className="panel-title">
            <span>
              Draft reply -- {ticket.org_name || "Unknown"} #
              {ticket.zoho_ticket_number}
            </span>
            <button className="panel-close" onClick={onDiscard}>
              X
            </button>
          </div>
        </div>

        {draftLoading && (
          <div className="panel-loading">Generating draft with Claude...</div>
        )}

        {draftError && <div className="panel-error">{draftError}</div>}

        {hasDraft && (
          <>
            {draft.language_detected && draft.language_detected !== "en" && (
              <div className="draft-lang-note">
                Language detected: {draft.language_detected}
              </div>
            )}

            <div className="draft-editor">
              <textarea
                className="draft-textarea"
                value={draft.draft}
                readOnly={!editing}
                onChange={(e) =>
                  onDraftChange({ ...draft, draft: e.target.value })
                }
                rows={12}
              />
            </div>

            <div className="draft-actions-row">
              <button
                className="action-btn"
                onClick={() => setShowRedraft(!showRedraft)}
                disabled={draftLoading}
              >
                Redraft
              </button>
              <button
                className="action-btn"
                onClick={() => setEditing(!editing)}
              >
                {editing ? "Lock" : "Edit manually"}
              </button>
              <button
                className="action-btn primary"
                onClick={() =>
                  onSend({
                    content: draft.draft,
                    zohoStatus,
                    clickupAction,
                  })
                }
                disabled={draftSending}
              >
                {draftSending ? "Sending..." : "Send"}
              </button>
              <button className="action-btn danger" onClick={onDiscard}>
                Discard
              </button>
            </div>

            {showRedraft && (
              <div className="redraft-row">
                <input
                  type="text"
                  className="redraft-input"
                  placeholder="e.g. Ask specifically about the passport upload, not login"
                  value={redraftText}
                  onChange={(e) => setRedraftText(e.target.value)}
                />
                <button
                  className="action-btn"
                  onClick={() => {
                    onGenerate({
                      draftType: draft.draft_type,
                      redraftInstruction: redraftText,
                    });
                    setShowRedraft(false);
                    setRedraftText("");
                  }}
                  disabled={draftLoading}
                >
                  Regenerate
                </button>
              </div>
            )}

            <div className="draft-sync-options">
              <div className="sync-option">
                <label>Status on send:</label>
                <select
                  value={zohoStatus}
                  onChange={(e) => setZohoStatus(e.target.value)}
                >
                  {ZOHO_STATUSES.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </div>
              <div className="sync-option">
                <label>ClickUp on send:</label>
                <select
                  value={clickupAction}
                  onChange={(e) => setClickupAction(e.target.value)}
                >
                  {CLICKUP_ACTIONS.map((a) => (
                    <option key={a.value} value={a.value}>
                      {a.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          </>
        )}

        {!hasDraft && !draftLoading && (
          <div className="draft-empty">
            <p>No draft generated yet.</p>
            <div className="draft-type-buttons">
              {[
                { type: "acknowledge", label: "Acknowledge" },
                { type: "request_info", label: "Request info" },
                { type: "resolution", label: "Resolution" },
                { type: "close", label: "Closure note" },
                { type: "admin_action", label: "Admin action" },
                { type: "escalation", label: "Escalation" },
              ].map((dt) => (
                <button
                  key={dt.type}
                  className="action-btn"
                  onClick={() => onGenerate({ draftType: dt.type })}
                >
                  {dt.label}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
