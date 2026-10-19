// Thin fetch wrapper. The token is a user session token, or an embed
// token when the app runs inside an embedding iframe.
let token = null;
try { token = localStorage.getItem("openbi.token"); } catch { /* storage may be blocked */ }

export function setToken(t, persist = true) {
  token = t;
  if (!persist) return;
  try {
    if (t) localStorage.setItem("openbi.token", t);
    else localStorage.removeItem("openbi.token");
  } catch { /* ignore */ }
}

export function getToken() { return token; }

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export async function api(method, path, body, { raw = false } = {}) {
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  const res = await fetch(path, { method, headers, body: payload });
  if (raw) {
    if (!res.ok) throw new ApiError(res.status, await res.text());
    return res;
  }
  if (res.status === 204) return null;
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const msg = (data && data.detail) ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : `${res.status} ${res.statusText}`;
    if (res.status === 401 && !path.includes("/auth/")) window.dispatchEvent(new CustomEvent("openbi:unauthorized"));
    throw new ApiError(res.status, msg);
  }
  return data;
}

export const get = (p) => api("GET", p);
export const post = (p, b) => api("POST", p, b ?? {});
export const put = (p, b) => api("PUT", p, b);
export const del = (p) => api("DELETE", p);
