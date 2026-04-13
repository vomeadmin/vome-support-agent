import { useState, useCallback } from "react";
import { fetchThread } from "../api";

export function useThread() {
  const [thread, setThread] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const load = useCallback(async (ticketId) => {
    if (!ticketId) return;
    setLoading(true);
    setError(null);
    try {
      const result = await fetchThread(ticketId);
      setThread(result);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  const clear = useCallback(() => {
    setThread(null);
    setError(null);
  }, []);

  return { thread, loading, error, load, clear };
}
