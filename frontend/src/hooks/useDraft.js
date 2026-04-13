import { useState, useCallback } from "react";
import { generateDraft, sendReply } from "../api";

export function useDraft() {
  const [draft, setDraft] = useState(null);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState(null);

  const generate = useCallback(async (ticketId, opts = {}) => {
    setLoading(true);
    setError(null);
    try {
      const result = await generateDraft(ticketId, {
        draft_type: opts.draftType || "request_info",
        redraft_instruction: opts.redraftInstruction || "",
        engineer_note: opts.engineerNote || "",
      });
      setDraft(result);
      return result;
    } catch (err) {
      setError(err.message);
      return null;
    } finally {
      setLoading(false);
    }
  }, []);

  const send = useCallback(async (ticketId, opts = {}) => {
    setSending(true);
    setError(null);
    try {
      const result = await sendReply(ticketId, {
        content: opts.content,
        zoho_status_after: opts.zohoStatus || "On Hold",
        clickup_action: opts.clickupAction || "leave",
        assignee_clickup_id: opts.assigneeClickupId || null,
        assignee_zoho_id: opts.assigneeZohoId || null,
      });
      return result;
    } catch (err) {
      setError(err.message);
      return null;
    } finally {
      setSending(false);
    }
  }, []);

  const clear = useCallback(() => {
    setDraft(null);
    setError(null);
  }, []);

  return { draft, loading, sending, error, generate, send, clear, setDraft };
}
