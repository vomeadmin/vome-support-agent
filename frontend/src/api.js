const API_BASE = "/ops";

function getToken() {
  return localStorage.getItem("ops_token") || "";
}

export function setToken(token) {
  localStorage.setItem("ops_token", token);
}

async function request(path, options = {}) {
  const token = getToken();
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...options.headers,
    },
  });
  if (res.status === 401) {
    throw new Error("Unauthorized - check your token");
  }
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API error ${res.status}: ${text}`);
  }
  return res.json();
}

export function fetchTickets(filter = "all", limit = 50) {
  return request(`/tickets?filter=${filter}&limit=${limit}`);
}

export function fetchThread(ticketId) {
  return request(`/ticket/${ticketId}/thread`);
}

export function generateDraft(ticketId, body) {
  return request(`/ticket/${ticketId}/draft`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function sendReply(ticketId, body) {
  return request(`/ticket/${ticketId}/send`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function assignTicket(ticketId, body) {
  return request(`/ticket/${ticketId}/assign`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function closeTicket(ticketId, body) {
  return request(`/ticket/${ticketId}/close`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function parkTicket(ticketId, body) {
  return request(`/ticket/${ticketId}/park`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}
