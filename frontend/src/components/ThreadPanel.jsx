import { useEffect, useState } from "react";

export default function ThreadPanel({ thread, loading, error, onClose }) {
  const [tab, setTab] = useState("thread");

  if (!thread && !loading) return null;

  return (
    <div className="panel-overlay" onClick={onClose}>
      <div className="thread-panel" onClick={(e) => e.stopPropagation()}>
        {loading && <div className="panel-loading">Loading thread...</div>}
        {error && <div className="panel-error">{error}</div>}
        {thread && (
          <>
            <div className="panel-header">
              <div className="panel-title">
                <span className="panel-org">
                  {thread.org_name || "Unknown"}
                </span>
                <span className="panel-number">
                  {" "}
                  -- #{thread.ticket_number}
                </span>
                <button className="panel-close" onClick={onClose}>
                  X
                </button>
              </div>
              <div className="panel-meta">
                <span>
                  {thread.contact_name} - {thread.contact_email}
                </span>
                {thread.tier && <span> - {thread.tier}</span>}
                {thread.arr > 0 && (
                  <span> - ${Number(thread.arr).toLocaleString()}/yr</span>
                )}
                {thread.assignee_name && (
                  <span> - {thread.assignee_name} assigned</span>
                )}
              </div>
            </div>

            <div className="panel-tabs">
              <button
                className={`panel-tab ${tab === "thread" ? "active" : ""}`}
                onClick={() => setTab("thread")}
              >
                Thread
              </button>
              <button
                className={`panel-tab ${tab === "clickup" ? "active" : ""}`}
                onClick={() => setTab("clickup")}
              >
                ClickUp notes ({thread.clickup_comments?.length || 0})
              </button>
            </div>

            <div className="panel-body">
              {tab === "thread" && (
                <div className="thread-messages">
                  {thread.threads?.length === 0 && (
                    <div className="empty">No messages yet</div>
                  )}
                  {thread.threads?.map((msg) => (
                    <div
                      key={msg.id}
                      className={`thread-msg ${msg.direction} ${
                        msg.is_internal ? "internal" : ""
                      }`}
                    >
                      <div className="msg-header">
                        <span className="msg-author">{msg.author}</span>
                        <span className="msg-direction">
                          {msg.is_internal
                            ? "Internal note"
                            : msg.direction === "inbound"
                            ? "Client"
                            : "Outbound"}
                        </span>
                        <span className="msg-time">
                          {msg.timestamp
                            ? new Date(msg.timestamp).toLocaleString()
                            : ""}
                        </span>
                      </div>
                      <div className="msg-content">
                        {msg.content || "(no content)"}
                      </div>
                      {msg.attachments?.length > 0 && (
                        <div className="msg-attachments">
                          {msg.attachments.map((att, i) => (
                            <a
                              key={i}
                              href={att.url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="attachment-link"
                            >
                              {att.name}
                            </a>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}

              {tab === "clickup" && (
                <div className="clickup-comments">
                  {thread.clickup_comments?.length === 0 && (
                    <div className="empty">No engineer notes</div>
                  )}
                  {thread.clickup_comments?.map((c, i) => (
                    <div key={i} className="cu-comment">
                      <div className="cu-comment-header">
                        <span className="cu-author">{c.author}</span>
                        <span className="cu-time">
                          {c.timestamp
                            ? new Date(c.timestamp).toLocaleString()
                            : ""}
                        </span>
                      </div>
                      <div className="cu-comment-text">{c.text}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
