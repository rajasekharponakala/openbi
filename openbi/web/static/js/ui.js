// Small UI helpers: modal dialogs, toasts and popup menus.
import { escapeHtml } from "./format.js";

export function modal(title, html, { wide = false } = {}) {
  const bg = document.createElement("div");
  bg.className = "modal-bg";
  bg.innerHTML = `<div class="modal" style="${wide ? "width:min(1100px,94vw)" : ""}"><div class="row"><h3 class="grow">${escapeHtml(title)}</h3><button class="icon" data-close>✕</button></div>${html}</div>`;
  document.body.appendChild(bg);
  const m = bg.querySelector(".modal");
  m.close = () => bg.remove();
  bg.addEventListener("mousedown", (e) => { if (e.target === bg) m.close(); });
  bg.querySelector("[data-close]").addEventListener("click", () => m.close());
  const first = m.querySelector("input, select, textarea");
  if (first) setTimeout(() => first.focus(), 0);
  return m;
}

export function toast(text, ms = 2200) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = text;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), ms);
}

export function menu(anchor, items) {
  document.querySelectorAll(".popup-menu").forEach((m) => m.remove());
  const r = anchor.getBoundingClientRect();
  const m = document.createElement("div");
  m.className = "popup-menu card";
  Object.assign(m.style, { position: "fixed", top: `${r.bottom + 2}px`, left: `${Math.max(4, r.right - 190)}px`, width: "190px", padding: "4px", zIndex: 40 });
  m.innerHTML = items.map(([label], i) => `<div data-i="${i}" style="padding:5px 8px;cursor:pointer;border-radius:4px">${escapeHtml(label)}</div>`).join("");
  m.querySelectorAll("[data-i]").forEach((el) => {
    el.addEventListener("mouseenter", () => { el.style.background = "var(--accent-soft)"; });
    el.addEventListener("mouseleave", () => { el.style.background = ""; });
    el.addEventListener("click", () => { m.remove(); items[Number(el.dataset.i)][1](); });
  });
  document.body.appendChild(m);
  setTimeout(() => document.addEventListener("mousedown", function off(e) {
    if (!m.contains(e.target)) { m.remove(); document.removeEventListener("mousedown", off); }
  }), 0);
}

export function statusBadge(status) {
  const cls = status === "ready" || status === "succeeded" ? "ok" : status === "failed" ? "bad" : status === "refreshing" || status === "running" ? "run" : "";
  return `<span class="badge ${cls}">${escapeHtml(status)}</span>`;
}
