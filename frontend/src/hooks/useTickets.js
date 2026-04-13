import { useState, useEffect, useCallback } from "react";
import { fetchTickets } from "../api";

export function useTickets(filter = "all", pollInterval = 60000) {
  const [data, setData] = useState({ tickets: [], stats: {}, total: 0 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const result = await fetchTickets(filter);
      setData(result);
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    setLoading(true);
    load();
    const id = setInterval(load, pollInterval);
    return () => clearInterval(id);
  }, [load, pollInterval]);

  return { ...data, loading, error, refresh: load };
}
