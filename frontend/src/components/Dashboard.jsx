import { useState, useCallback } from "react";
import { useTickets } from "../hooks/useTickets";
import { useThread } from "../hooks/useThread";
import { useDraft } from "../hooks/useDraft";
import { assignTicket, closeTicket, parkTicket } from "../api";
import FilterBar from "./FilterBar";
import TicketCard from "./TicketCard";
import ThreadPanel from "./ThreadPanel";
import DraftPanel from "./DraftPanel";

export default function Dashboard() {
  const [filter, setFilter] = useState("all");
  const { tickets, stats, total, loading, error, refresh } = useTickets(filter);

  // Thread panel state
  const {
    thread,
    loading: threadLoading,
    error: threadError,
    load: loadThread,
    clear: clearThread,
  } = useThread();

  // Draft panel state
  const [draftTicket, setDraftTicket] = useState(null);
  const {
    draft,
    loading: draftLoading,
    sending: draftSending,
    error: draftError,
    generate: generateDraft,
    send: sendDraft,
    clear: clearDraft,
    setDraft,
  } = useDraft();

  // Action feedback
  const [actionMsg, setActionMsg] = useState(null);

  const showMessage = useCallback((msg, isError = false) => {
    setActionMsg({ text: msg, isError });
    setTimeout(() => setActionMsg(null), 4000);
  }, []);

  const handleAction = useCallback(
    async (action, ticket) => {
      switch (action) {
        case "thread":
          loadThread(ticket.zoho_ticket_id);
          break;

        case "draft":
          setDraftTicket(ticket);
          clearDraft();
          break;

        case "request_info":
          setDraftTicket(ticket);
          generateDraft(ticket.zoho_ticket_id, {
            draftType: "request_info",
          });
          break;

        case "assign_sanjay":
        case "assign_onlyg": {
          const eng = action === "assign_sanjay" ? "sanjay" : "onlyg";
          try {
            const result = await assignTicket(ticket.zoho_ticket_id, {
              engineer: eng,
              send_ack: true,
            });
            showMessage(result.message || `Assigned to ${eng}`);
            refresh();
          } catch (err) {
            showMessage(err.message, true);
          }
          break;
        }

        case "close":
          if (confirm("Close this ticket?")) {
            try {
              const result = await closeTicket(ticket.zoho_ticket_id, {
                send_closure_note: true,
                resolution: "completed",
              });
              showMessage(result.message || "Ticket closed");
              refresh();
            } catch (err) {
              showMessage(err.message, true);
            }
          }
          break;

        case "park": {
          const note = prompt("Park note (optional):");
          const wake = prompt("Wake date (YYYY-MM-DD, optional):");
          try {
            const result = await parkTicket(ticket.zoho_ticket_id, {
              note: note || "",
              wake_date: wake || null,
            });
            showMessage(result.message || "Ticket parked");
            refresh();
          } catch (err) {
            showMessage(err.message, true);
          }
          break;
        }

        default:
          break;
      }
    },
    [loadThread, generateDraft, clearDraft, refresh, showMessage]
  );

  const handleDraftGenerate = useCallback(
    (opts) => {
      if (draftTicket) {
        generateDraft(draftTicket.zoho_ticket_id, opts);
      }
    },
    [draftTicket, generateDraft]
  );

  const handleDraftSend = useCallback(
    async (opts) => {
      if (!draftTicket) return;
      const result = await sendDraft(draftTicket.zoho_ticket_id, opts);
      if (result?.success) {
        showMessage(result.message || "Reply sent");
        setDraftTicket(null);
        clearDraft();
        refresh();
      } else if (result) {
        showMessage(result.message || "Send failed", true);
      }
    },
    [draftTicket, sendDraft, clearDraft, refresh, showMessage]
  );

  return (
    <div className="dashboard">
      <header className="dashboard-header">
        <h1>Vome Command Center</h1>
        <div className="header-right">
          <span className="ticket-count">{total} tickets</span>
          <button className="refresh-btn" onClick={refresh} disabled={loading}>
            {loading ? "Loading..." : "Refresh"}
          </button>
        </div>
      </header>

      {actionMsg && (
        <div className={`toast ${actionMsg.isError ? "error" : "success"}`}>
          {actionMsg.text}
        </div>
      )}

      {error && <div className="dashboard-error">{error}</div>}

      <FilterBar
        activeFilter={filter}
        onFilterChange={setFilter}
        stats={stats}
      />

      <div className="ticket-list">
        {loading && tickets.length === 0 && (
          <div className="loading-state">Loading tickets...</div>
        )}
        {!loading && tickets.length === 0 && (
          <div className="empty-state">No tickets match this filter.</div>
        )}
        {tickets.map((t) => (
          <TicketCard
            key={t.zoho_ticket_id}
            ticket={t}
            onAction={handleAction}
          />
        ))}
      </div>

      {/* Thread slide-in panel */}
      {(thread || threadLoading) && (
        <ThreadPanel
          thread={thread}
          loading={threadLoading}
          error={threadError}
          onClose={clearThread}
        />
      )}

      {/* Draft panel */}
      {draftTicket && (
        <DraftPanel
          ticket={draftTicket}
          draft={draft}
          draftLoading={draftLoading}
          draftSending={draftSending}
          draftError={draftError}
          onGenerate={handleDraftGenerate}
          onSend={handleDraftSend}
          onDiscard={() => {
            setDraftTicket(null);
            clearDraft();
          }}
          onDraftChange={setDraft}
        />
      )}
    </div>
  );
}
