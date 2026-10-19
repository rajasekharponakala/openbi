// Dataset (semantic model) editor: tables & measures, Power Query steps
// with live preview, relationships, row-level security and refresh.
import { api, get, post, put } from "./api.js";
import { escapeHtml } from "./format.js";
import { statusBadge, toast } from "./ui.js";

const STEP_TEMPLATES = {
  filterRows: { op: "filterRows", column: "Column", operator: "equals", value: "x" },
  selectColumns: { op: "selectColumns", columns: ["A", "B"] },
  removeColumns: { op: "removeColumns", columns: ["A"] },
  renameColumns: { op: "renameColumns", mapping: { Old: "New" } },
  changeType: { op: "changeType", types: { Column: "double" } },
  addColumn: { op: "addColumn", name: "New column", expression: "\"A\" * 2", dataType: "double" },
  replaceValues: { op: "replaceValues", column: "Column", find: "old", replace: "new" },
  trim: { op: "trim", columns: ["Column"] },
  fillNull: { op: "fillNull", column: "Column", value: 0 },
  removeDuplicates: { op: "removeDuplicates", columns: ["Key"] },
  sort: { op: "sort", by: [{ column: "Column", descending: true }] },
  keepTop: { op: "keepTop", count: 100 },
  groupBy: { op: "groupBy", keys: ["Key"], aggregations: [{ name: "Total", op: "sum", column: "Amount" }] },
  merge: { op: "merge", table: "Other", leftOn: "Key", rightOn: "Key", kind: "left", expand: ["Name"] },
  append: { op: "append", tables: ["Other"] },
  unpivot: { op: "unpivot", columns: ["Jan", "Feb"], attribute: "Month", value: "Amount" },
  splitColumn: { op: "splitColumn", column: "Name", delimiter: " ", into: ["First", "Last"] },
  nativeQuery: { op: "nativeQuery", query: "SELECT * FROM _prev" },
};

export class DatasetView {
  constructor(root, id) {
    this.root = root;
    this.id = id;
    this.tab = "model";
    this.table = null;
    this.step = null;
  }

  async load() {
    this.ds = await get(`/api/datasets/${this.id}`);
    if (!this.ds.definition) {
      this.root.innerHTML = `<div class="page"><h1>${escapeHtml(this.ds.name)}</h1><p class="muted">You need Contributor access to edit this dataset.</p></div>`;
      return;
    }
    this.def = JSON.parse(JSON.stringify(this.ds.definition));
    try { this.schema = await get(`/api/datasets/${this.id}/schema`); } catch { this.schema = { tables: [] }; }
    this.table = this.table || this.def.tables[0]?.name || null;
    this.render();
  }

  physical(table) { return this.schema.tables.find((t) => t.name === table)?.columns || []; }

  tdef(name = this.table) { return this.def.tables.find((t) => t.name === name); }

  dirty() { this.isDirty = true; const b = this.root.querySelector("[data-act=save]"); if (b) b.textContent = "Save*"; }

  render() {
    const tabs = [["model", "Model"], ["query", "Power Query"], ["rels", "Relationships"], ["security", "Security"], ["refresh", "Refresh"], ["json", "JSON"]];
    this.root.innerHTML = `<div class="page" style="max-width:1300px">
      <div class="row"><h1 class="grow">${escapeHtml(this.ds.name)} ${statusBadge(this.ds.status)}</h1>
        <button data-act="save" class="primary">Save${this.isDirty ? "*" : ""}</button><button data-act="refresh">Refresh now</button>
        <a href="#/ws/${this.ds.workspaceId}">Back to workspace</a></div>
      <div class="tabs-h">${tabs.map(([k, l]) => `<button data-tab="${k}" class="${this.tab === k ? "on" : ""}">${l}</button>`).join("")}</div>
      <div id="tabbody"></div><div class="error" id="err"></div></div>`;
    this.root.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => { this.tab = b.dataset.tab; this.render(); }));
    this.root.querySelector("[data-act=save]").addEventListener("click", () => this.save());
    this.root.querySelector("[data-act=refresh]").addEventListener("click", () => this.refresh());
    const body = this.root.querySelector("#tabbody");
    ({ model: () => this.renderModel(body), query: () => this.renderQuery(body), rels: () => this.renderRels(body),
       security: () => this.renderSecurity(body), refresh: () => this.renderRefresh(body), json: () => this.renderJson(body) })[this.tab]();
  }

  error(e) { this.root.querySelector("#err").textContent = e ? (e.message || String(e)) : ""; }

  async save() {
    try {
      this.ds = await put(`/api/datasets/${this.id}`, { definition: this.def });
      this.isDirty = false;
      toast("Dataset saved");
      this.render();
    } catch (e) { this.error(e); }
  }

  async refresh() {
    try {
      if (this.isDirty) await this.save();
      toast("Refreshing…");
      await post(`/api/datasets/${this.id}/refresh`);
      toast("Refresh succeeded");
    } catch (e) { toast("Refresh failed"); this.error(e); }
    await this.load();
  }

  tableList(onPick, extra = "") {
    return `<div class="list">${this.def.tables.map((t) => `<div class="item" style="cursor:pointer;${t.name === this.table ? "background:var(--accent-soft)" : ""}" data-pick="${escapeHtml(t.name)}">
      <span class="grow">${escapeHtml(t.name)}</span><span class="muted" style="font-size:11px">${t.mode === "directQuery" ? "DirectQuery" : ""}</span></div>`).join("")}</div>${extra}`;
  }

  bindPick(body) {
    body.querySelectorAll("[data-pick]").forEach((el) => el.addEventListener("click", () => { this.table = el.dataset.pick; this.step = null; this.render(); }));
  }

  // ------------------------------------------------------------ model --
  renderModel(body) {
    const t = this.tdef();
    if (!t) { body.innerHTML = `<p class="muted">No tables yet. Add one in the Power Query tab.</p>`; return; }
    const phys = this.physical(t.name);
    const meta = new Map((t.columns || []).map((c) => [c.name, c]));
    body.innerHTML = `<div class="two"><div>${this.tableList()}</div><div>
      <h2>Columns</h2>
      <div class="scroll" style="max-height:260px"><table class="grid"><thead><tr><th>Name</th><th>Type</th><th>Hidden</th><th>Format</th><th>Sort by</th><th>Expression (calculated)</th></tr></thead><tbody>
      ${phys.map((c) => { const m = meta.get(c.name) || {}; return `<tr><td>${escapeHtml(c.name)}</td><td class="mono">${escapeHtml(c.type)}</td>
        <td><input type="checkbox" data-hide="${escapeHtml(c.name)}" ${m.isHidden ? "checked" : ""}></td>
        <td><input data-fmt="${escapeHtml(c.name)}" value="${escapeHtml(m.formatString || "")}" style="width:90px"></td>
        <td><input data-sort="${escapeHtml(c.name)}" value="${escapeHtml(m.sortByColumn || "")}" style="width:100px"></td>
        <td class="mono">${escapeHtml(m.expression || "")}</td></tr>`; }).join("") || '<tr><td colspan="6" class="muted">Refresh the dataset to see columns.</td></tr>'}
      </tbody></table></div>
      <h2>Calculated columns <span class="muted" style="font-size:12px;font-weight:400">(DAX row expressions, evaluated at refresh)</span></h2>
      ${(t.columns || []).filter((c) => c.expression).map((c) => `<div class="row" style="margin-bottom:4px"><input value="${escapeHtml(c.name)}" data-cc-name="${escapeHtml(c.name)}" style="width:160px"><input class="grow mono" data-cc-expr="${escapeHtml(c.name)}" value="${escapeHtml(c.expression)}"><button class="icon" data-cc-del="${escapeHtml(c.name)}">×</button></div>`).join("")}
      <button data-act="addcc">+ Calculated column</button>
      <h2>Measures <span class="muted" style="font-size:12px;font-weight:400">(DAX)</span></h2>
      <div id="measures">${(t.measures || []).map((m, i) => `<div class="card" style="padding:8px;margin-bottom:6px">
          <div class="row"><input data-m-name="${i}" value="${escapeHtml(m.name)}" style="font-weight:600;width:220px"><input data-m-fmt="${i}" value="${escapeHtml(m.formatString || "")}" placeholder="Format e.g. $#,0" style="width:130px">
          <span class="grow"></span><button data-m-test="${i}">Test</button><button class="icon" data-m-del="${i}">×</button></div>
          <textarea data-m-expr="${i}" rows="2" style="width:100%;margin-top:4px">${escapeHtml(m.expression)}</textarea><div class="mono muted" data-m-out="${i}"></div></div>`).join("")}</div>
      <button data-act="addm">+ New measure</button>
    </div></div>`;
    this.bindPick(body);
    const colMeta = (name) => {
      t.columns = t.columns || [];
      let c = t.columns.find((x) => x.name === name);
      if (!c) { c = { name }; t.columns.push(c); }
      return c;
    };
    body.querySelectorAll("[data-hide]").forEach((cb) => cb.addEventListener("change", () => { colMeta(cb.dataset.hide).isHidden = cb.checked || undefined; this.dirty(); }));
    body.querySelectorAll("[data-fmt]").forEach((i) => i.addEventListener("change", () => { colMeta(i.dataset.fmt).formatString = i.value || undefined; this.dirty(); }));
    body.querySelectorAll("[data-sort]").forEach((i) => i.addEventListener("change", () => { colMeta(i.dataset.sort).sortByColumn = i.value || undefined; this.dirty(); }));
    body.querySelectorAll("[data-cc-expr]").forEach((i) => i.addEventListener("change", () => { colMeta(i.dataset.ccExpr).expression = i.value; this.dirty(); }));
    body.querySelectorAll("[data-cc-name]").forEach((i) => i.addEventListener("change", () => { colMeta(i.dataset.ccName).name = i.value; this.dirty(); this.render(); }));
    body.querySelectorAll("[data-cc-del]").forEach((b) => b.addEventListener("click", () => { t.columns = t.columns.filter((c) => c.name !== b.dataset.ccDel); this.dirty(); this.render(); }));
    body.querySelector("[data-act=addcc]").addEventListener("click", () => {
      const name = prompt("Column name");
      if (!name) return;
      colMeta(name).expression = `${quoteTable(t.name)}[${phys[0]?.name || "Column"}]`;
      this.dirty(); this.render();
    });
    body.querySelector("[data-act=addm]").addEventListener("click", () => {
      t.measures = t.measures || [];
      const col = phys.find((c) => /INT|DOUBLE|DECIMAL/.test(c.type));
      t.measures.push({ name: `New measure ${t.measures.length + 1}`, expression: col ? `SUM(${quoteTable(t.name)}[${col.name}])` : `COUNTROWS(${quoteTable(t.name)})` });
      this.dirty(); this.render();
    });
    const ms = t.measures || [];
    body.querySelectorAll("[data-m-name]").forEach((i) => i.addEventListener("change", () => { ms[Number(i.dataset.mName)].name = i.value; this.dirty(); }));
    body.querySelectorAll("[data-m-fmt]").forEach((i) => i.addEventListener("change", () => { ms[Number(i.dataset.mFmt)].formatString = i.value || undefined; this.dirty(); }));
    body.querySelectorAll("[data-m-expr]").forEach((i) => i.addEventListener("change", () => { ms[Number(i.dataset.mExpr)].expression = i.value; this.dirty(); }));
    body.querySelectorAll("[data-m-del]").forEach((b) => b.addEventListener("click", () => { ms.splice(Number(b.dataset.mDel), 1); this.dirty(); this.render(); }));
    body.querySelectorAll("[data-m-test]").forEach((b) => b.addEventListener("click", async () => {
      const i = Number(b.dataset.mTest);
      const out = body.querySelector(`[data-m-out="${i}"]`);
      try {
        const expr = body.querySelector(`[data-m-expr="${i}"]`).value;
        const res = await post(`/api/datasets/${this.id}/query`, { select: [{ expression: expr, name: "value" }] });
        out.textContent = `= ${JSON.stringify(res.rows[0]?.[0])}   (measures referencing other measures use the saved model)`;
      } catch (e) { out.textContent = e.message; }
    }));
  }

  // ------------------------------------------------------ power query --
  renderQuery(body) {
    const t = this.tdef();
    body.innerHTML = `<div class="two"><div>${this.tableList()}
        <div style="margin-top:10px" class="row"><button data-act="newfile">+ From file</button><button data-act="newsql">+ From SQL</button></div>
        <h2>Files</h2><div id="files" class="muted">…</div>
        <label>Upload data file (CSV, Parquet, JSON, XLSX)</label><input type="file" id="up"></div>
      <div>${t ? `
        <div class="row"><h2 class="grow" style="margin-top:0">${escapeHtml(t.name)}</h2>
          <label class="row" style="margin:0;color:inherit">Storage mode <select data-act="mode"><option value="import">Import</option><option value="directQuery" ${t.mode === "directQuery" ? "selected" : ""}>DirectQuery</option></select></label>
          <button class="danger" data-act="deltable">Delete table</button></div>
        <label>Source</label><textarea id="src" rows="3" style="width:100%">${escapeHtml(JSON.stringify(t.source, null, 1))}</textarea>
        <div class="row" style="margin-top:8px;align-items:flex-start;gap:16px">
          <div style="width:260px"><div class="section-title">Applied steps</div><div class="steps">
            <div class="step ${this.step === 0 ? "on" : ""}" data-step="0">Source</div>
            ${(t.steps || []).map((s, i) => `<div class="step ${this.step === i + 1 ? "on" : ""}" data-step="${i + 1}">${i + 1}. ${escapeHtml(s.op)}</div>`).join("")}
          </div>
          <div class="row" style="margin-top:6px"><select id="tpl"><option value="">+ Add step…</option>${Object.keys(STEP_TEMPLATES).map((k) => `<option>${k}</option>`).join("")}</select></div></div>
          <div class="grow"><div class="section-title">Steps (JSON)</div><textarea id="steps" rows="9" style="width:100%">${escapeHtml(JSON.stringify(t.steps || [], null, 1))}</textarea>
          <div class="row"><button data-act="apply">Apply & preview</button><span class="muted" style="font-size:12px">Steps compile to SQL and fold into one DuckDB query.</span></div></div>
        </div>
        <h2>Preview</h2><div id="preview" class="scroll"><div class="muted" style="padding:8px">Select a step to preview.</div></div>`
      : '<p class="muted">Add a table from a file or a SQL query.</p>'}</div></div>`;
    this.bindPick(body);
    this.loadFiles(body);
    body.querySelector("#up").addEventListener("change", async (e) => {
      const f = e.target.files[0];
      if (!f) return;
      const fd = new FormData();
      fd.append("file", f);
      try { await api("POST", `/api/datasets/${this.id}/files`, fd); toast("Uploaded"); this.loadFiles(body); } catch (err) { this.error(err); }
    });
    body.querySelector("[data-act=newfile]").addEventListener("click", async () => {
      const files = await get(`/api/datasets/${this.id}/files`);
      const path = prompt(`File path (uploaded files: ${files.map((x) => x.path).join(", ") || "none"})`, files[0]?.path || "data/");
      if (!path) return;
      const ext = path.split(".").pop().toLowerCase();
      const kind = { csv: "csv", tsv: "csv", txt: "csv", parquet: "parquet", json: "json", jsonl: "json", xlsx: "excel" }[ext] || "csv";
      const name = prompt("Table name", path.split("/").pop().replace(/\.[^.]+$/, "")) || "Table";
      this.def.tables.push({ name, source: { kind, path }, steps: [] });
      this.table = name; this.step = 0; this.dirty(); this.render();
    });
    body.querySelector("[data-act=newsql]").addEventListener("click", () => {
      const name = prompt("Table name", "Calendar");
      if (!name) return;
      this.def.tables.push({ name, source: { kind: "sql", query: "SELECT CAST(range AS DATE) AS \"Date\" FROM range(DATE '2024-01-01', DATE '2025-01-01', INTERVAL 1 DAY)" }, steps: [] });
      this.table = name; this.step = 0; this.dirty(); this.render();
    });
    if (!t) return;
    const readEditors = () => {
      try {
        t.source = JSON.parse(body.querySelector("#src").value);
        t.steps = JSON.parse(body.querySelector("#steps").value);
        this.error(null);
        return true;
      } catch (e) { this.error(`Invalid JSON: ${e.message}`); return false; }
    };
    body.querySelector("[data-act=mode]").addEventListener("change", (e) => { t.mode = e.target.value; this.dirty(); });
    body.querySelector("[data-act=deltable]").addEventListener("click", () => {
      if (!confirm(`Delete table ${t.name}?`)) return;
      this.def.tables = this.def.tables.filter((x) => x !== t);
      this.def.relationships = (this.def.relationships || []).filter((r) => r.fromTable !== t.name && r.toTable !== t.name);
      this.table = this.def.tables[0]?.name || null; this.dirty(); this.render();
    });
    body.querySelector("#tpl").addEventListener("change", (e) => {
      if (!e.target.value || !readEditors()) return;
      t.steps = [...(t.steps || []), JSON.parse(JSON.stringify(STEP_TEMPLATES[e.target.value]))];
      this.step = t.steps.length; this.dirty(); this.render();
    });
    body.querySelector("[data-act=apply]").addEventListener("click", () => {
      if (!readEditors()) return;
      this.dirty();
      this.step = (t.steps || []).length;
      this.render();
    });
    body.querySelectorAll("[data-step]").forEach((s) => s.addEventListener("click", () => { if (readEditors()) { this.step = Number(s.dataset.step); this.render(); } }));
    if (this.step !== null) this.preview(body, t);
  }

  async loadFiles(body) {
    try {
      const files = await get(`/api/datasets/${this.id}/files`);
      body.querySelector("#files").innerHTML = files.map((f) => `<div class="mono">${escapeHtml(f.path)} <span class="muted">(${Math.round(f.bytes / 1024)} KB)</span></div>`).join("") || "No files";
    } catch (e) { body.querySelector("#files").textContent = e.message; }
  }

  async preview(body, t) {
    const box = body.querySelector("#preview");
    box.innerHTML = '<div class="muted" style="padding:8px">Loading…</div>';
    try {
      const res = await post(`/api/datasets/${this.id}/preview`, { table: t.name, upto: this.step, limit: 100, definition: this.def });
      box.innerHTML = `<table class="grid"><thead><tr>${res.columns.map((c) => `<th>${escapeHtml(c.name)}<div class="muted mono" style="font-weight:400">${escapeHtml(c.type)}</div></th>`).join("")}</tr></thead>
        <tbody>${res.rows.map((r) => `<tr>${r.map((v) => `<td>${v === null ? '<span class="muted">null</span>' : escapeHtml(v)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
    } catch (e) { box.innerHTML = `<div class="error" style="padding:8px">${escapeHtml(e.message)}</div>`; }
  }

  // ---------------------------------------------------- relationships --
  renderRels(body) {
    const rels = (this.def.relationships = this.def.relationships || []);
    const tables = this.def.tables.map((t) => t.name);
    const colOpts = (t) => this.physical(t).map((c) => `<option>${escapeHtml(c.name)}</option>`).join("");
    body.innerHTML = `<p class="muted">Filters flow from the one side (dimension) to the many side (fact). Set cross-filter to <b>both</b> to let a fact filter its dimension.</p>
      <table class="grid card" style="padding:0"><thead><tr><th>From (many)</th><th>To (one)</th><th>Cross filter</th><th>Active</th><th></th></tr></thead><tbody>
      ${rels.map((r, i) => `<tr><td>${escapeHtml(r.fromTable)}[${escapeHtml(r.fromColumn)}]</td><td>${escapeHtml(r.toTable)}[${escapeHtml(r.toColumn)}]</td>
        <td><select data-cf="${i}"><option value="single">single</option><option value="both" ${r.crossFilteringBehavior === "both" ? "selected" : ""}>both</option></select></td>
        <td><input type="checkbox" data-act-r="${i}" ${r.isActive === false ? "" : "checked"}></td><td><button class="icon" data-rdel="${i}">×</button></td></tr>`).join("")}
      </tbody></table>
      <h2>New relationship</h2>
      <div class="row"><select id="ft">${tables.map((t) => `<option>${escapeHtml(t)}</option>`).join("")}</select><select id="fc"></select> →
        <select id="tt">${tables.map((t) => `<option>${escapeHtml(t)}</option>`).join("")}</select><select id="tc"></select><button id="radd">Add</button></div>`;
    const sync = () => {
      body.querySelector("#fc").innerHTML = colOpts(body.querySelector("#ft").value);
      body.querySelector("#tc").innerHTML = colOpts(body.querySelector("#tt").value);
    };
    body.querySelector("#ft").addEventListener("change", sync);
    body.querySelector("#tt").addEventListener("change", sync);
    sync();
    body.querySelector("#radd").addEventListener("click", () => {
      rels.push({ fromTable: body.querySelector("#ft").value, fromColumn: body.querySelector("#fc").value, toTable: body.querySelector("#tt").value, toColumn: body.querySelector("#tc").value });
      this.dirty(); this.render();
    });
    body.querySelectorAll("[data-rdel]").forEach((b) => b.addEventListener("click", () => { rels.splice(Number(b.dataset.rdel), 1); this.dirty(); this.render(); }));
    body.querySelectorAll("[data-cf]").forEach((s) => s.addEventListener("change", () => { rels[Number(s.dataset.cf)].crossFilteringBehavior = s.value; this.dirty(); }));
    body.querySelectorAll("[data-act-r]").forEach((c) => c.addEventListener("change", () => { rels[Number(c.dataset.actR)].isActive = c.checked; this.dirty(); }));
  }

  // --------------------------------------------------------- security --
  renderSecurity(body) {
    const roles = (this.def.roles = this.def.roles || []);
    const tables = this.def.tables.map((t) => t.name);
    body.innerHTML = `<p class="muted">Row-level security roles filter rows for workspace <b>Viewers</b> and embed tokens. Use <span class="mono">USERPRINCIPALNAME()</span> for dynamic security. Authors can test a role with "View as" in the report.</p>
      ${roles.map((r, i) => `<div class="card" style="margin-bottom:10px"><div class="row"><input data-rn="${i}" value="${escapeHtml(r.name)}" style="font-weight:600"><span class="grow"></span><button class="icon" data-rdel="${i}">×</button></div>
        ${(r.tablePermissions || []).map((p, j) => `<div class="row" style="margin-top:6px"><select data-pt="${i}:${j}">${tables.map((t) => `<option ${t === p.table ? "selected" : ""}>${escapeHtml(t)}</option>`).join("")}</select>
          <input class="grow mono" data-pe="${i}:${j}" value="${escapeHtml(p.filterExpression)}"><button class="icon" data-pdel="${i}:${j}">×</button></div>`).join("")}
        <button data-padd="${i}" style="margin-top:6px">+ Table filter</button>
        <label>Members (emails, comma separated)</label><input data-rm="${i}" style="width:100%" value="${escapeHtml((r.members || []).join(", "))}"></div>`).join("")}
      <button id="addrole">+ New role</button>`;
    const ij = (s) => s.split(":").map(Number);
    body.querySelector("#addrole").addEventListener("click", () => { roles.push({ name: `Role ${roles.length + 1}`, tablePermissions: [], members: [] }); this.dirty(); this.render(); });
    body.querySelectorAll("[data-rn]").forEach((x) => x.addEventListener("change", () => { roles[Number(x.dataset.rn)].name = x.value; this.dirty(); }));
    body.querySelectorAll("[data-rdel]").forEach((x) => x.addEventListener("click", () => { roles.splice(Number(x.dataset.rdel), 1); this.dirty(); this.render(); }));
    body.querySelectorAll("[data-rm]").forEach((x) => x.addEventListener("change", () => { roles[Number(x.dataset.rm)].members = x.value.split(",").map((s) => s.trim()).filter(Boolean); this.dirty(); }));
    body.querySelectorAll("[data-padd]").forEach((x) => x.addEventListener("click", () => {
      const r = roles[Number(x.dataset.padd)];
      (r.tablePermissions = r.tablePermissions || []).push({ table: tables[0], filterExpression: `${quoteTable(tables[0])}[Column] = "value"` });
      this.dirty(); this.render();
    }));
    body.querySelectorAll("[data-pt]").forEach((x) => x.addEventListener("change", () => { const [i, j] = ij(x.dataset.pt); roles[i].tablePermissions[j].table = x.value; this.dirty(); }));
    body.querySelectorAll("[data-pe]").forEach((x) => x.addEventListener("change", () => { const [i, j] = ij(x.dataset.pe); roles[i].tablePermissions[j].filterExpression = x.value; this.dirty(); }));
    body.querySelectorAll("[data-pdel]").forEach((x) => x.addEventListener("click", () => { const [i, j] = ij(x.dataset.pdel); roles[i].tablePermissions.splice(j, 1); this.dirty(); this.render(); }));
  }

  // ---------------------------------------------------------- refresh --
  async renderRefresh(body) {
    const lr = this.ds.lastRefresh;
    body.innerHTML = `<div class="card"><div class="row"><b class="grow">Scheduled refresh</b></div>
        <label>Cron expression (UTC), e.g. <span class="mono">0 6 * * *</span> = daily at 06:00</label>
        <div class="row"><input id="cron" class="mono" value="${escapeHtml(this.ds.refreshSchedule || "")}" placeholder="disabled"><button id="sched">Save schedule</button></div></div>
      <h2>Last refresh</h2>
      ${lr ? `<div class="card">${statusBadge(lr.status)} <span class="muted">${escapeHtml(lr.at || "")}</span> ${lr.error ? `<div class="error">${escapeHtml(lr.error)}</div>` : ""}
        ${lr.tables ? `<table class="grid" style="margin-top:8px"><thead><tr><th>Table</th><th>Mode</th><th>Rows</th><th>Seconds</th></tr></thead><tbody>${lr.tables.map((t) => `<tr><td>${escapeHtml(t.name)}</td><td>${escapeHtml(t.mode)}</td><td>${t.rows ?? "-"}</td><td>${t.seconds.toFixed(2)}</td></tr>`).join("")}</tbody></table>` : ""}</div>` : '<p class="muted">Never refreshed.</p>'}
      <h2>History</h2><div id="hist" class="muted">…</div>`;
    body.querySelector("#sched").addEventListener("click", async () => {
      try { this.ds = await put(`/api/datasets/${this.id}/schedule`, { cron: body.querySelector("#cron").value.trim() || null }); toast("Schedule saved"); } catch (e) { this.error(e); }
    });
    const hist = await get(`/api/datasets/${this.id}/refreshes`);
    body.querySelector("#hist").innerHTML = `<table class="grid"><thead><tr><th>Started</th><th>Trigger</th><th>Status</th><th>Error</th></tr></thead><tbody>
      ${hist.map((h) => `<tr><td>${escapeHtml(h.startedAt)}</td><td>${escapeHtml(h.trigger)}</td><td>${statusBadge(h.status)}</td><td class="error">${escapeHtml(h.error || "")}</td></tr>`).join("")}</tbody></table>`;
  }

  renderJson(body) {
    body.innerHTML = `<p class="muted">The full semantic model definition (also stored as model.yaml in project folders).</p>
      <textarea id="json" rows="28" style="width:100%">${escapeHtml(JSON.stringify(this.def, null, 2))}</textarea>
      <button id="applyjson">Apply</button>`;
    body.querySelector("#applyjson").addEventListener("click", () => {
      try { this.def = JSON.parse(body.querySelector("#json").value); this.dirty(); toast("Applied - save to persist"); } catch (e) { this.error(e); }
    });
  }
}

function quoteTable(name) { return /^[A-Za-z_][A-Za-z0-9_]*$/.test(name) ? name : `'${name.replace(/'/g, "''")}'`; }
