import ActionButtons from "./ActionButtons";

const P_COLORS = { P1: "#ef4444", P2: "#f59e0b", P3: "#6b7280" };
const TIER_COLORS = {
  Enterprise: "#3b82f6",
  Ultimate: "#8b5cf6",
  Pro: "#22c55e",
  Recruit: "#6b7280",
  Prospect: "#6b7280",
  Unknown: "#6b7280",
};

const STATUS_LABELS = {
  new: "New",
  processing: "Processing",
  needs_review: "Needs review",
  final_review: "Final review",
  waiting: "Waiting on client",
};

const STATUS_COLORS = {
  new: "#ef4444",
  processing: "#f59e0b",
  needs_review: "#ef4444",
  final_review: "#8b5cf6",
  waiting: "#f59e0b",
};

function Badge({ label, color, outline }) {
  const style = outline
    ? { border: `1px solid ${color}`, color }
    : { background: color, color: "#fff" };
  return (
    <span className="badge" style={style}>
      {label}
    </span>
  );
}

export default function TicketCard({ ticket, onAction }) {
  const pColor = P_COLORS[ticket.p_level] || P_COLORS.P3;

  return (
    <div className="ticket-card" style={{ borderLeftColor: pColor }}>
      <div className="card-header">
        <div className="card-badges">
          <Badge label={ticket.p_level} color={pColor} />
          {ticket.tier && ticket.tier !== "Unknown" && (
            <Badge
              label={ticket.tier}
              color={TIER_COLORS[ticket.tier] || "#6b7280"}
            />
          )}
          <Badge
            label={
              STATUS_LABELS[ticket.zoho_status_normalized] ||
              ticket.zoho_status
            }
            color={
              STATUS_COLORS[ticket.zoho_status_normalized] || "#6b7280"
            }
          />
        </div>
        <div className="card-meta">
          <span className="ticket-number">#{ticket.zoho_ticket_number}</span>
          {ticket.clickup_link && (
            <a
              href={ticket.clickup_link}
              target="_blank"
              rel="noopener noreferrer"
              className="cu-link"
              title="Open in ClickUp"
            >
              CU
            </a>
          )}
        </div>
      </div>

      <div className="card-body">
        <div className="card-org">{ticket.org_name || "Unknown org"}</div>
        <div className="card-subject">{ticket.subject}</div>
        {ticket.summary && (
          <div className="card-summary">{ticket.summary}</div>
        )}
      </div>

      {ticket.missing_info && (
        <div className="card-callout warning">
          <span className="callout-icon">!</span> Need: {ticket.missing_info}
        </div>
      )}

      {ticket.engineer_comment && (
        <div className="card-callout engineer">
          <span className="callout-icon">E</span> {ticket.engineer_comment}
        </div>
      )}

      <div className="card-tags">
        {ticket.module && <span className="tag">{ticket.module}</span>}
        {ticket.platform && <span className="tag">{ticket.platform}</span>}
        <span className="tag muted">
          {ticket.days_since_update === 0
            ? "Today"
            : ticket.days_since_update === 1
            ? "1 day ago"
            : `${ticket.days_since_update} days ago`}
        </span>
        <span
          className={`tag ${ticket.assignee_name ? "" : "danger"}`}
        >
          {ticket.assignee_name || "Unassigned"}
        </span>
        {ticket.arr_dollars > 0 && (
          <span className="tag">
            ${ticket.arr_dollars.toLocaleString()}/yr
          </span>
        )}
      </div>

      <ActionButtons ticket={ticket} onAction={onAction} />
    </div>
  );
}
