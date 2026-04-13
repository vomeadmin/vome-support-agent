export default function ActionButtons({ ticket, onAction }) {
  const status = ticket.zoho_status_normalized;
  const cuStatus = ticket.clickup_status;

  return (
    <div className="action-buttons">
      <button
        className="action-btn primary"
        onClick={() => onAction("draft", ticket)}
      >
        Draft reply
      </button>
      <button
        className="action-btn"
        onClick={() => onAction("thread", ticket)}
      >
        View thread
      </button>
      {!ticket.assignee_clickup_id && (
        <>
          <button
            className="action-btn assign"
            onClick={() => onAction("assign_sanjay", ticket)}
          >
            Assign Sanjay
          </button>
          <button
            className="action-btn assign"
            onClick={() => onAction("assign_onlyg", ticket)}
          >
            Assign OnlyG
          </button>
        </>
      )}
      {ticket.assignee_clickup_id && (
        <button
          className="action-btn"
          onClick={() =>
            onAction(
              ticket.assignee_name?.includes("Sanjay")
                ? "assign_onlyg"
                : "assign_sanjay",
              ticket
            )
          }
        >
          Reassign
        </button>
      )}
      <button
        className="action-btn"
        onClick={() => onAction("request_info", ticket)}
      >
        Request info
      </button>
      <button
        className="action-btn"
        onClick={() => onAction("park", ticket)}
      >
        Park
      </button>
      <button
        className="action-btn danger"
        onClick={() => onAction("close", ticket)}
      >
        Close
      </button>
    </div>
  );
}
