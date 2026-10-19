// Application shell and hash router.
import { del, get, getToken, post, put, setToken, api } from "./api.js";
import { DashboardView } from "./dashboard.js";
import { DatasetView } from "./dataset.js";
import { escapeHtml } from "./format.js";
import { ReportView } from "./report.js";
import { modal, statusBadge, toast } from "./ui.js";

const main = document.getElementById("main");
const crumbs = document.getElementById("crumbs");
let current = null;
let me = null;

window.addEventListener("openbi:unauthorized", () => { setToken(null); location.hash = "#/login"; });
window.addEventListener("hashchange", route);
window.addEventListener("beforeunload", (e) => { if (current?.dirty || current?.isDirty) e.preventDefault(); });
document.getElementById("logout").addEventListener("click", () => { setToken(null); me = null; location.hash = "#/login"; });

function setCrumbs(parts) {
  crumbs.innerHTML = parts.map(([label, href]) => (href ? `<a href="${href}">${escapeHtml(label)}</a>` : `<span>${escapeHtml(label)}</span>`)).join("<span>›</span>");
}

async function route() {
  if (current?.destroy) current.destroy();
  current = null;
  const [path, qs] = location.hash.replace(/^#/, "").split("?");
  const params = new URLSearchParams(qs || "");
  const parts = path.split("/").filter(Boolean);
  if (!getToken() && parts[0] !== "login") { location.hash = "#/login"; return; }
  if (parts[0] === "login") return loginPage();
  try {
    if (!me) me = await get("/api/me");
    document.getElementById("who").textContent = me.email || "";
    if (parts.length === 0) return await homePage();
    if (parts[0] === "ws") return await workspacePage(Number(parts[1]));
    if (parts[0] === "report") return await reportPage(Number(parts[1]), params);
    if (parts[0] === "dataset") return await datasetPage(Number(parts[1]));
    if (parts[0] === "dashboard") return await dashboardPage(Number(parts[1]));
    main.innerHTML = `<div class="page"><h1>Not found</h1></div>`;
  } catch (e) {
    main.innerHTML = `<div class="page"><div class="error">${escapeHtml(e.message)}</div></div>`;
  }
}

function loginPage() {
  setCrumbs([]);
  main.innerHTML = `<div class="login card"><h1 style="margin-top:0">Sign in to OpenBI</h1>
    <form id="lf"><label>Email</label><input name="email" autocomplete="username" required>
    <label>Password</label><input name="password" type="password" autocomplete="current-password" required>
    <button class="primary" style="margin-top:14px;width:100%">Sign in</button><div class="error" id="le"></div></form></div>`;
  main.querySelector("#lf").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const res = await post("/api/auth/login", { email: f.get("email"), password: f.get("password") });
      setToken(res.token);
      me = null;
      location.hash = "#/";
    } catch (err) { main.querySelector("#le").textContent = err.message; }
  });
}

async function homePage() {
  setCrumbs([["Workspaces"]]);
  const wss = await get("/api/workspaces");
  main.innerHTML = `<div class="page"><div class="row"><h1 class="grow">Workspaces</h1><button class="primary" id="nw">New workspace</button></div>
    <div class="list">${wss.map((w) => `<div class="item"><span class="kind">Workspace</span><a class="grow" href="#/ws/${w.id}">${escapeHtml(w.name)}</a><span class="muted">${escapeHtml(w.description)}</span><span class="badge">${escapeHtml(w.role)}</span></div>`).join("") || '<div class="item muted">No workspaces yet.</div>'}</div></div>`;
  main.querySelector("#nw").addEventListener("click", async () => {
    const name = prompt("Workspace name");
    if (!name) return;
    try { const w = await post("/api/workspaces", { name }); location.hash = `#/ws/${w.id}`; } catch (e) { toast(e.message); }
  });
}

async function workspacePage(id) {
  const ws = await get(`/api/workspaces/${id}`);
  setCrumbs([["Workspaces", "#/"], [ws.name]]);
  const canEdit = ["Admin", "Member", "Contributor"].includes(ws.role);
  const ds = (d) => `<div class="item"><span class="kind">Dataset</span>${canEdit ? `<a class="grow" href="#/dataset/${d.id}">${escapeHtml(d.name)}</a>` : `<span class="grow">${escapeHtml(d.name)}</span>`}
      ${statusBadge(d.status)}<span class="muted" style="font-size:12px">${d.refreshSchedule ? `⏱ ${escapeHtml(d.refreshSchedule)}` : ""}</span>
      ${canEdit ? `<button data-refresh="${d.id}">Refresh</button><button data-newreport="${d.id}">New report</button><button class="icon danger" data-delds="${d.id}" title="Delete">🗑</button>` : ""}</div>`;
  const rep = (r) => `<div class="item"><span class="kind">Report</span><a class="grow" href="#/report/${r.id}">${escapeHtml(r.name)}</a>
      ${canEdit ? `<a href="#/report/${r.id}?edit=1">Edit</a><button class="icon danger" data-delrep="${r.id}" title="Delete">🗑</button>` : ""}</div>`;
  const dash = (d) => `<div class="item"><span class="kind">Dashboard</span><a class="grow" href="#/dashboard/${d.id}">${escapeHtml(d.name)}</a>
      ${canEdit ? `<button class="icon danger" data-deldash="${d.id}" title="Delete">🗑</button>` : ""}</div>`;
  main.innerHTML = `<div class="page"><div class="row"><h1 class="grow">${escapeHtml(ws.name)}</h1><span class="badge">${escapeHtml(ws.role)}</span>
      ${canEdit ? '<button class="primary" id="getdata">Get data</button>' : ""}</div>
    <p class="muted">${escapeHtml(ws.description || "")}</p>
    <h2>Reports</h2><div class="list">${ws.reports.map(rep).join("") || '<div class="item muted">No reports.</div>'}</div>
    <h2>Dashboards</h2><div class="list">${ws.dashboards.map(dash).join("") || '<div class="item muted">No dashboards. Pin a visual from a report to create one.</div>'}</div>
    <h2>Datasets</h2><div class="list">${ws.datasets.map(ds).join("") || '<div class="item muted">No datasets.</div>'}</div>
    ${ws.members ? `<h2>Access</h2><div class="list">${ws.members.map((m) => `<div class="item"><span class="grow">${escapeHtml(m.name || m.email)} <span class="muted">${escapeHtml(m.email)}</span></span><span class="badge">${escapeHtml(m.role)}</span></div>`).join("")}</div>
      <div class="row" style="margin-top:8px"><input id="memail" placeholder="user@example.com"><select id="mrole"><option>Viewer</option><option>Contributor</option><option>Member</option><option>Admin</option><option>None</option></select><button id="madd">Grant access</button></div>` : ""}
  </div>`;
  const on = (sel, fn) => main.querySelectorAll(sel).forEach((b) => b.addEventListener("click", () => fn(b)));
  on("[data-refresh]", async (b) => {
    b.disabled = true; b.textContent = "Refreshing…";
    try { await post(`/api/datasets/${b.dataset.refresh}/refresh`); toast("Refresh succeeded"); } catch (e) { toast(`Refresh failed: ${e.message}`, 5000); }
    workspacePage(id);
  });
  on("[data-newreport]", async (b) => {
    const name = prompt("Report name", "New report");
    if (!name) return;
    const r = await post(`/api/workspaces/${id}/reports`, { name, datasetId: Number(b.dataset.newreport) });
    location.hash = `#/report/${r.id}?edit=1`;
  });
  on("[data-delds]", async (b) => { if (confirm("Delete dataset and its reports?")) { await del(`/api/datasets/${b.dataset.delds}`); workspacePage(id); } });
  on("[data-delrep]", async (b) => { if (confirm("Delete report?")) { await del(`/api/reports/${b.dataset.delrep}`); workspacePage(id); } });
  on("[data-deldash]", async (b) => { if (confirm("Delete dashboard?")) { await del(`/api/dashboards/${b.dataset.deldash}`); workspacePage(id); } });
  main.querySelector("#madd")?.addEventListener("click", async () => {
    try { await put(`/api/workspaces/${id}/members`, { email: main.querySelector("#memail").value, role: main.querySelector("#mrole").value }); workspacePage(id); } catch (e) { toast(e.message); }
  });
  main.querySelector("#getdata")?.addEventListener("click", () => getDataDialog(id));
}

function getDataDialog(workspaceId) {
  const m = modal("Get data", `<p class="muted">Upload a file to create a dataset with one table (you can add more tables, measures and relationships in the dataset editor).</p>
    <label>Dataset name</label><input id="gn" style="width:100%" value="My dataset">
    <label>File (CSV, Parquet, JSON or XLSX)</label><input type="file" id="gf">
    <div class="row" style="margin-top:14px"><span class="grow"></span><button class="primary" id="gg">Load</button></div><div class="error" id="ge"></div>`);
  m.querySelector("#gg").addEventListener("click", async () => {
    const file = m.querySelector("#gf").files[0];
    const name = m.querySelector("#gn").value || "Dataset";
    if (!file) return;
    try {
      const ds = await post(`/api/workspaces/${workspaceId}/datasets`, { name, definition: { name, tables: [] } });
      const fd = new FormData();
      fd.append("file", file);
      const up = await api("POST", `/api/datasets/${ds.id}/files`, fd);
      const ext = up.name.split(".").pop().toLowerCase();
      const kind = { parquet: "parquet", json: "json", jsonl: "json", xlsx: "excel" }[ext] || "csv";
      const table = up.name.replace(/\.[^.]+$/, "").replace(/[^\w ]+/g, " ").trim() || "Table";
      await put(`/api/datasets/${ds.id}`, { definition: { name, tables: [{ name: table, source: { kind, path: up.path } }] } });
      await post(`/api/datasets/${ds.id}/refresh`);
      const r = await post(`/api/workspaces/${workspaceId}/reports`, { name: `${name} report`, datasetId: ds.id });
      m.close();
      location.hash = `#/report/${r.id}?edit=1`;
    } catch (e) { m.querySelector("#ge").textContent = e.message; }
  });
}

async function reportPage(id, params) {
  main.innerHTML = "";
  const view = new ReportView(main, { reportId: id, startEditing: params.get("edit") === "1" });
  current = view;
  await view.load();
  const ws = await get(`/api/workspaces/${view.meta.workspaceId}`).catch(() => null);
  setCrumbs([["Workspaces", "#/"], [ws?.name || "Workspace", `#/ws/${view.meta.workspaceId}`], [view.meta.name]]);
  if (params.get("page")) { try { view.setPage(params.get("page")); } catch { /* ignore */ } }
}

async function datasetPage(id) {
  const view = new DatasetView(main, id);
  current = view;
  await view.load();
  setCrumbs([["Workspaces", "#/"], ["Workspace", `#/ws/${view.ds.workspaceId}`], [view.ds.name]]);
}

async function dashboardPage(id) {
  const view = new DashboardView(main, id);
  current = view;
  await view.load();
  setCrumbs([["Workspaces", "#/"], ["Workspace", `#/ws/${view.dash.workspaceId}`], [view.dash.name]]);
}

route();
