// Report canvas: pages, visuals, slicers, cross-filtering, the filter pane
// and (for authors) the designer with Visualizations / Fields panes.
import { api, get, post, put } from "./api.js";
import {
  AGGREGATES, advancedFilter, basicFilter, columnRef, describeFilter, filterTargetLabel,
  measureRef, outputName, parseRef, selectItem, topNFilter,
} from "./fields.js";
import { escapeHtml } from "./format.js";
import { DEFAULT_THEME, VISUALS, buildQueries, disposeChart, renderVisual } from "./visuals.js";
import { modal, toast, menu } from "./ui.js";

/* global GridStack */

const NUMERIC = /INT|DOUBLE|DECIMAL|FLOAT|REAL|NUMERIC|HUGEINT/i;
const KEYLIKE = /(year|key|id|number|code|month|day|quarter)$/i;
let uid = 0;

export class ReportView {
  constructor(root, { reportId, embed = false, settings = {}, onEvent = () => {}, startEditing = false }) {
    this.root = root;
    this.reportId = reportId;
    this.embed = embed;
    this.settings = { filterPaneEnabled: true, navContentPaneEnabled: true, ...settings };
    this.onEvent = onEvent;
    this.editing = false;
    this.startEditing = startEditing;
    this.pageIdx = 0;
    this.selected = null; // selected visual name (designer / filter pane)
    this.cross = null; // {visual, key, filters}
    this.slicers = {}; // visual name -> values
    this.extraFilters = []; // set through the embedding API
    this.viewAs = null; // {roles, user}
    this.activeWell = null;
    this.dirty = false;
    this.seq = {};
    this.valueCache = new Map();
  }

  // ------------------------------------------------------------ load --
  async load() {
    const meta = await get(`/api/reports/${this.reportId}`);
    this.meta = meta;
    this.report = meta.definition || {};
    this.report.pages = this.report.pages?.length ? this.report.pages : [{ name: "page1", displayName: "Page 1", visuals: [] }];
    this.report.filters = this.report.filters || [];
    this.theme = { ...DEFAULT_THEME, ...(this.report.theme || {}) };
    this.schema = await get(`/api/datasets/${meta.datasetId}/schema`);
    this.formats = new Map();
    for (const t of this.schema.tables) {
      for (const m of t.measures) this.formats.set(measureRef(m.name), m.formatString);
      for (const c of t.columns) if (c.formatString) this.formats.set(columnRef(t.name, c.name), c.formatString);
    }
    for (const p of this.report.pages) for (const v of p.visuals) if (v.state?.values) this.slicers[v.name] = v.state.values;
    this.build();
    if (this.startEditing && meta.canEdit) this.setEditing(true);
    this.onEvent("loaded", { reportId: this.reportId, pages: this.getPages() });
    this.showPage(0, { silent: true });
  }

  formatOf(ref) {
    const p = parseRef(ref);
    if (p.kind === "measure") return this.formats.get(ref);
    if (p.kind === "column") return this.formats.get(ref) || "General";
    return this.formats.get(columnRef(p.table, p.column));
  }

  get page() { return this.report.pages[this.pageIdx]; }

  visualByName(name) { return this.page.visuals.find((v) => v.name === name); }

  // ----------------------------------------------------------- shell --
  build() {
    const r = this.root;
    r.innerHTML = `
      <div class="report">
        <div class="canvas-wrap">
          <div class="toolbar ${this.embed && !this.meta.canEdit ? "hidden" : ""}">
            <span class="title">${escapeHtml(this.meta.name)}</span>
            <button data-act="edit" class="${this.meta.canEdit ? "" : "hidden"}">Edit</button>
            <button data-act="save" class="primary hidden">Save</button>
            <button data-act="addpage" class="hidden">+ Page</button>
            <span class="grow"></span>
            <button data-act="reset" title="Clear selections and slicers">Reset</button>
            <select data-act="viewas" class="${this.meta.canEdit && !this.embed && this.schema.roles.length ? "" : "hidden"}" title="View as role">
              <option value="">View as: me</option>
              ${this.schema.roles.map((x) => `<option value="${escapeHtml(x)}">Role: ${escapeHtml(x)}</option>`).join("")}
            </select>
            <button data-act="embed" class="${this.meta.canEdit && !this.embed ? "" : "hidden"}">Embed</button>
          </div>
          <div class="canvas"><div class="grid-stack"></div></div>
          <div class="tabs ${this.settings.navContentPaneEnabled ? "" : "hidden"}"></div>
        </div>
        <div class="pane filters ${this.settings.filterPaneEnabled ? "" : "hidden"}">
          <div class="pane-head"><span>Filters</span><span class="grow"></span><button class="icon" data-act="collapse">⟩</button></div>
          <div class="pane-body"></div>
        </div>
        <div class="pane viz hidden"><div class="pane-head"><span>Visualizations</span></div><div class="pane-body"></div></div>
        <div class="pane fields hidden"><div class="pane-head"><span>Data</span></div><div class="pane-body"></div></div>
      </div>`;
    this.el = r.querySelector(".report");
    this.gridEl = r.querySelector(".grid-stack");
    this.grid = GridStack.init({ column: 12, cellHeight: 70, margin: 5, float: true, staticGrid: true, animate: false, minRow: 9 }, this.gridEl);
    this.grid.on("change", (_e, items) => {
      for (const it of items || []) {
        const v = this.visualByName(it.id);
        if (v) Object.assign(v, { x: it.x, y: it.y, w: it.w, h: it.h });
      }
      this.markDirty();
    });
    r.querySelector(".toolbar").addEventListener("click", (e) => this.onToolbar(e));
    r.querySelector("[data-act=viewas]").addEventListener("change", (e) => {
      this.viewAs = e.target.value ? { roles: [e.target.value] } : null;
      if (this.viewAs && this.schema.roles.length) {
        const user = prompt("USERPRINCIPALNAME() to test with (optional)", "") || undefined;
        this.viewAs.user = user;
      }
      this.valueCache.clear();
      this.refreshAll();
    });
    r.querySelector(".pane.filters [data-act=collapse]").addEventListener("click", (e) => {
      const pane = e.target.closest(".pane");
      pane.classList.toggle("collapsed");
      e.target.textContent = pane.classList.contains("collapsed") ? "⟨" : "⟩";
    });
    this.keyHandler = (e) => {
      if (!this.editing || !this.selected) return;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
      if (e.key === "Delete" || e.key === "Backspace") this.deleteVisual(this.selected);
    };
    document.addEventListener("keydown", this.keyHandler);
    this.gridEl.addEventListener("click", (e) => {
      if (e.target === this.gridEl && this.editing) this.select(null);
    });
  }

  destroy() {
    document.removeEventListener("keydown", this.keyHandler);
    this.gridEl.querySelectorAll(".vbody").forEach(disposeChart);
    this.grid?.destroy(false);
  }

  async onToolbar(e) {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act === "edit") this.setEditing(!this.editing);
    if (act === "save") await this.save();
    if (act === "addpage") this.addPage();
    if (act === "reset") { this.cross = null; this.slicers = {}; this.refreshAll(); }
    if (act === "embed") this.embedDialog();
  }

  setEditing(on) {
    this.editing = on;
    this.el.classList.toggle("editing", on);
    this.grid.setStatic(!on);
    const q = (s) => this.root.querySelector(s);
    q("[data-act=edit]").textContent = on ? "Reading view" : "Edit";
    q("[data-act=save]").classList.toggle("hidden", !on);
    q("[data-act=addpage]").classList.toggle("hidden", !on);
    q(".pane.viz").classList.toggle("hidden", !on);
    q(".pane.fields").classList.toggle("hidden", !on);
    if (on) { this.renderFieldsPane(); this.renderVizPane(); }
    this.renderTabs();
    this.renderFilterPane();
  }

  markDirty() {
    if (!this.editing) return;
    this.dirty = true;
    const b = this.root.querySelector("[data-act=save]");
    if (b) b.textContent = "Save*";
  }

  async save() {
    for (const p of this.report.pages) for (const v of p.visuals) {
      if (v.type === "slicer") v.state = { values: this.slicers[v.name] || [] };
    }
    this.report.theme = this.report.theme || undefined;
    await put(`/api/reports/${this.reportId}`, { definition: this.report });
    this.dirty = false;
    this.root.querySelector("[data-act=save]").textContent = "Save";
    toast("Report saved");
    this.onEvent("saved", { reportId: this.reportId });
  }

  // ----------------------------------------------------------- pages --
  getPages() {
    return this.report.pages.map((p, i) => ({ name: p.name, displayName: p.displayName || p.name, isActive: i === this.pageIdx }));
  }

  setPage(name) {
    const i = this.report.pages.findIndex((p) => p.name === name);
    if (i < 0) throw new Error(`page '${name}' not found`);
    this.showPage(i);
  }

  renderTabs() {
    const tabs = this.root.querySelector(".tabs");
    tabs.innerHTML = this.report.pages.map((p, i) => `<div class="tab ${i === this.pageIdx ? "active" : ""}" data-i="${i}">${escapeHtml(p.displayName || p.name)}${this.editing && this.report.pages.length > 1 && i === this.pageIdx ? ' <button class="icon" data-del title="Delete page">×</button>' : ""}</div>`).join("");
    tabs.querySelectorAll(".tab").forEach((t) => {
      t.addEventListener("click", (e) => {
        const i = Number(t.dataset.i);
        if (e.target.dataset.del !== undefined) return this.deletePage(i);
        if (i !== this.pageIdx) this.showPage(i);
      });
      t.addEventListener("dblclick", () => {
        if (!this.editing) return;
        const p = this.report.pages[Number(t.dataset.i)];
        const name = prompt("Page name", p.displayName || p.name);
        if (name) { p.displayName = name; this.markDirty(); this.renderTabs(); }
      });
    });
  }

  addPage() {
    const n = this.report.pages.length + 1;
    this.report.pages.push({ name: `page${Date.now().toString(36)}`, displayName: `Page ${n}`, filters: [], visuals: [] });
    this.markDirty();
    this.showPage(this.report.pages.length - 1);
  }

  deletePage(i) {
    if (!confirm("Delete this page?")) return;
    this.report.pages.splice(i, 1);
    this.markDirty();
    this.showPage(Math.max(0, i - 1));
  }

  showPage(i, { silent = false } = {}) {
    this.pageIdx = i;
    this.cross = null;
    this.selected = null;
    this.gridEl.querySelectorAll(".vbody").forEach(disposeChart);
    this.grid.removeAll(true);
    for (const v of this.page.visuals) this.addWidget(v);
    this.renderTabs();
    this.renderFilterPane();
    if (this.editing) this.renderVizPane();
    this.refreshAll();
    if (!silent) this.onEvent("pageChanged", { newPage: { name: this.page.name, displayName: this.page.displayName } });
  }

  addWidget(v) {
    const el = this.grid.addWidget({ x: v.x ?? 0, y: v.y ?? 0, w: v.w ?? 4, h: v.h ?? 3, id: v.name });
    const content = el.querySelector(".grid-stack-item-content");
    content.innerHTML = `<div class="visual" data-name="${escapeHtml(v.name)}">
        <div class="vhead"><div class="vtitle"></div><div class="vmenu"><button data-m="more" title="More options">⋯</button></div></div>
        <div class="vbody"></div></div>`;
    const ve = content.querySelector(".visual");
    ve.addEventListener("mousedown", () => { if (this.editing) this.select(v.name); });
    ve.addEventListener("click", () => { if (!this.editing && this.selected !== v.name) { this.selected = v.name; this.renderFilterPane(); } });
    ve.querySelector("[data-m=more]").addEventListener("click", (e) => { e.stopPropagation(); this.visualMenu(v, e.target); });
    this.updateTitle(v);
    return el;
  }

  updateTitle(v) {
    const t = this.visualEl(v.name)?.querySelector(".vtitle");
    if (!t) return;
    const auto = this.autoTitle(v);
    t.textContent = v.title ?? auto;
    t.parentElement.style.display = (v.type === "card" && !v.title) || v.format?.hideTitle ? "none" : "";
  }

  autoTitle(v) {
    const f = v.fields || {};
    const vals = [...(f.values || []), ...(f.lineValues || []), ...(f.x || []), ...(f.y || [])].map((r) => parseRef(r).label);
    const cats = [...(f.category || []), ...(f.rows || []), ...(f.field || [])].map((r) => parseRef(r).label);
    if (v.type === "slicer") return cats[0] || "Slicer";
    if (v.type === "table") return "";
    if (v.type === "textbox") return "";
    if (!vals.length) return VISUALS[v.type]?.label || v.type;
    return cats.length ? `${vals.join(", ")} by ${cats.join(", ")}` : vals.join(", ");
  }

  visualEl(name) { return this.gridEl.querySelector(`.visual[data-name="${CSS.escape(name)}"]`); }

  select(name) {
    this.selected = name;
    this.gridEl.querySelectorAll(".visual").forEach((e) => e.classList.toggle("selected", e.dataset.name === name));
    this.activeWell = null;
    this.renderVizPane();
    this.renderFilterPane();
  }

  // --------------------------------------------------------- filters --
  slicerFilter(v) {
    const values = this.slicers[v.name];
    const ref = v.fields?.field?.[0];
    if (!ref || !values || !values.length) return null;
    const p = parseRef(ref);
    return basicFilter(p.table, p.column, values);
  }

  filtersFor(v) {
    const out = [...this.report.filters, ...this.extraFilters, ...(this.page.filters || []), ...(v.filters || [])];
    for (const other of this.page.visuals) {
      if (other.type === "slicer" && other.name !== v.name) {
        const f = this.slicerFilter(other);
        if (f) out.push(f);
      }
    }
    if (this.cross && this.cross.visual !== v.name && v.type !== "slicer") out.push(...this.cross.filters);
    return out.filter((f) => !(f.filterType === 1 && f.operator === "All"));
  }

  queryBody(q, filters) {
    const body = { ...q, filters };
    if (this.viewAs) { body.asRoles = this.viewAs.roles; if (this.viewAs.user) body.asUser = this.viewAs.user; }
    return body;
  }

  async query(q, filters) {
    return post(`/api/datasets/${this.meta.datasetId}/query`, this.queryBody(q, filters));
  }

  // --------------------------------------------------------- refresh --
  refreshAll() {
    for (const v of this.page.visuals) this.refreshVisual(v);
  }

  async refreshVisual(v) {
    const ve = this.visualEl(v.name);
    if (!ve) return;
    const body = ve.querySelector(".vbody");
    this.updateTitle(v);
    const queries = buildQueries(v);
    if (!queries) {
      disposeChart(body);
      if (v.type === "textbox") return renderVisual(body, v, {}, this.ctx(v));
      const roles = VISUALS[v.type]?.roles.map((r) => r.label).join(", ");
      body.innerHTML = `<div class="vmsg">${this.editing ? `Add data fields: ${escapeHtml(roles)}` : ""}</div>`;
      return;
    }
    const seq = (this.seq[v.name] = (this.seq[v.name] || 0) + 1);
    const filters = this.filtersFor(v);
    try {
      const entries = await Promise.all(Object.entries(queries).map(async ([k, q]) => [k, await this.query(q, filters)]));
      if (seq !== this.seq[v.name]) return;
      const data = Object.fromEntries(entries);
      v._sql = data.main.sql;
      renderVisual(body, v, data, this.ctx(v));
      this.onEvent("visualRendered", { name: v.name });
    } catch (err) {
      if (seq !== this.seq[v.name]) return;
      disposeChart(body);
      body.innerHTML = `<div class="vmsg err">${escapeHtml(err.message)}</div>`;
      this.onEvent("error", { visual: v.name, message: err.message });
    }
  }

  ctx(v) {
    return {
      theme: this.theme,
      formatOf: (ref) => this.formatOf(ref),
      selectedKey: this.cross && this.cross.visual === v.name ? this.cross.key : null,
      slicerValues: this.slicers[v.name] || [],
      onSelect: (sel) => {
        this.cross = sel ? { visual: v.name, ...sel } : null;
        for (const other of this.page.visuals) this.refreshVisual(other);
        this.onEvent("dataSelected", { visual: { name: v.name, type: v.type }, filters: sel ? sel.filters : [] });
      },
      onSlicer: (values) => {
        this.slicers[v.name] = values;
        this.markDirty();
        for (const other of this.page.visuals) if (other.name !== v.name) this.refreshVisual(other);
        this.onEvent("filtersApplied", { visual: v.name, values });
      },
    };
  }

  // ----------------------------------------------------- visual menu --
  visualMenu(v, anchor) {
    const items = [
      ["Export data (CSV)", () => this.exportData(v, "csv")],
      ["Export data (Excel)", () => this.exportData(v, "xlsx")],
      ["Focus mode", () => this.focus(v)],
    ];
    if (!this.embed) items.push(["Pin to dashboard", () => this.pin(v)]);
    if (this.meta.canEdit && v._sql) items.push(["Show query (SQL)", () => modal("Generated SQL", `<pre class="mono" style="white-space:pre-wrap">${escapeHtml(v._sql)}</pre>`)]);
    if (this.editing) {
      items.push(["Duplicate", () => this.duplicate(v)]);
      items.push(["Delete", () => this.deleteVisual(v.name)]);
    }
    menu(anchor, items);
  }

  async exportData(v, format) {
    const q = buildQueries(v)?.main;
    if (!q) return toast("Nothing to export");
    const res = await api("POST", `/api/datasets/${this.meta.datasetId}/export`, { ...this.queryBody({ ...q, top: undefined }, this.filtersFor(v)), format }, { raw: true });
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${(v.title || this.autoTitle(v) || v.name).replace(/[^\w -]+/g, "")}.${format}`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    this.onEvent("exportDataCompleted", { visual: v.name, format });
  }

  focus(v) {
    const m = modal(v.title ?? this.autoTitle(v), `<div style="position:relative;height:65vh"><div class="vbody" style="position:absolute;inset:0"></div></div>`, { wide: true });
    const body = m.querySelector(".vbody");
    const queries = buildQueries(v);
    if (!queries) return;
    const filters = this.filtersFor(v);
    Promise.all(Object.entries(queries).map(async ([k, q]) => [k, await this.query(q, filters)]))
      .then((entries) => renderVisual(body, v, Object.fromEntries(entries), { ...this.ctx(v), onSelect: () => {}, onSlicer: () => {} }))
      .catch((e) => { body.innerHTML = `<div class="vmsg err">${escapeHtml(e.message)}</div>`; });
  }

  async pin(v) {
    if (this.dirty) await this.save();
    const ws = await get(`/api/workspaces/${this.meta.workspaceId}`);
    const options = ws.dashboards.map((d) => `<option value="${d.id}">${escapeHtml(d.name)}</option>`).join("");
    const m = modal("Pin to dashboard", `
      <label>Dashboard</label><select id="pd" style="width:100%"><option value="">New dashboard…</option>${options}</select>
      <label>New dashboard name</label><input id="pn" style="width:100%" value="${escapeHtml(this.meta.name)} dashboard">
      <div class="row" style="margin-top:14px"><span class="grow"></span><button class="primary" id="pg">Pin</button></div>`);
    m.querySelector("#pg").addEventListener("click", async () => {
      const id = m.querySelector("#pd").value;
      const tile = { reportId: this.reportId, page: this.page.name, visual: v.name, w: Math.min(v.w || 4, 6), h: v.h || 3 };
      let dash;
      if (id) {
        dash = await get(`/api/dashboards/${id}`);
        const y = Math.max(0, ...dash.tiles.map((t) => (t.y || 0) + (t.h || 3)));
        await put(`/api/dashboards/${id}`, { name: dash.name, tiles: [...dash.tiles.map(({ datasetId, ...t }) => t), { ...tile, x: 0, y }] });
      } else {
        dash = await post(`/api/workspaces/${this.meta.workspaceId}/dashboards`, { name: m.querySelector("#pn").value, tiles: [{ ...tile, x: 0, y: 0 }] });
      }
      m.close();
      toast("Pinned to dashboard");
    });
  }

  // ------------------------------------------------------ designer ops --
  newVisual(type) {
    const name = `v${Date.now().toString(36)}${uid++}`;
    const maxY = Math.max(0, ...this.page.visuals.map((v) => (v.y || 0) + (v.h || 3)));
    const v = { name, type, x: 0, y: maxY, w: type === "card" || type === "slicer" ? 3 : 6, h: type === "card" ? 2 : 4, fields: {} };
    this.page.visuals.push(v);
    this.addWidget(v);
    this.markDirty();
    this.select(name);
    this.refreshVisual(v);
    return v;
  }

  duplicate(v) {
    const copy = JSON.parse(JSON.stringify({ ...v, _sql: undefined }));
    copy.name = `v${Date.now().toString(36)}${uid++}`;
    copy.y = (v.y || 0) + (v.h || 3);
    this.page.visuals.push(copy);
    this.addWidget(copy);
    this.markDirty();
    this.refreshVisual(copy);
  }

  deleteVisual(name) {
    const i = this.page.visuals.findIndex((v) => v.name === name);
    if (i < 0) return;
    const el = this.grid.getGridItems().find((it) => it.gridstackNode?.id === name);
    if (el) { disposeChart(el.querySelector(".vbody")); this.grid.removeWidget(el); }
    this.page.visuals.splice(i, 1);
    delete this.slicers[name];
    if (this.cross?.visual === name) this.cross = null;
    this.selected = null;
    this.markDirty();
    this.renderVizPane();
    this.refreshAll();
  }

  changeType(v, type) {
    const roles = VISUALS[type].roles;
    const old = v.fields || {};
    const pool = Object.values(old).flat();
    const fields = {};
    for (const r of roles) {
      fields[r.name] = [];
      for (const ref of pool) {
        if (fields[r.name].length >= r.max) break;
        if (Object.values(fields).flat().includes(ref)) continue;
        const isVal = parseRef(ref).kind !== "column";
        if (r.accepts === "any" || (r.accepts === "value") === isVal) fields[r.name].push(ref);
      }
    }
    v.type = type;
    v.fields = fields;
    this.markDirty();
    this.renderVizPane();
    this.refreshVisual(v);
  }

  addField(table, column, isMeasure, typeName) {
    let v = this.selected && this.visualByName(this.selected);
    if (!v) v = this.newVisual(isMeasure || NUMERIC.test(typeName || "") ? "columnChart" : "table");
    const roles = VISUALS[v.type].roles;
    const numeric = !isMeasure && NUMERIC.test(typeName || "") && !KEYLIKE.test(column);
    let role = roles.find((r) => r.name === this.activeWell);
    if (!role) {
      const free = (r) => (v.fields?.[r.name] || []).length < r.max;
      const wantsValue = isMeasure || numeric;
      // prefer the natural well (values for measures/numbers, axes for text), then any compatible one
      role = roles.find((r) => free(r) && (r.accepts === "any" || (r.accepts === "value") === wantsValue))
        || roles.find((r) => free(r) && (r.accepts !== "column" || !isMeasure))
        || roles[0];
    }
    if (!role) return;
    let ref;
    if (isMeasure) ref = measureRef(column);
    else if (role.accepts === "value" || (role.accepts === "any" && numeric)) ref = { field: columnRef(table, column), aggregate: NUMERIC.test(typeName || "") ? "sum" : "count" };
    else ref = columnRef(table, column);
    if (role.accepts === "column" && isMeasure) return toast(`${role.label} needs a column`);
    v.fields = v.fields || {};
    const list = (v.fields[role.name] = v.fields[role.name] || []);
    if (list.length >= role.max) list.splice(role.max - 1, 1);
    list.push(ref);
    this.markDirty();
    this.renderVizPane();
    this.refreshVisual(v);
  }

  // ------------------------------------------------------------ panes --
  renderFieldsPane() {
    const body = this.root.querySelector(".pane.fields .pane-body");
    body.innerHTML = `<input class="search" placeholder="Search" style="width:100%;margin-bottom:6px"><div class="fields"></div>`;
    const list = body.querySelector(".fields");
    const draw = (q = "") => {
      list.innerHTML = this.schema.tables.filter((t) => !t.isHidden).map((t) => {
        const ms = t.measures.filter((m) => !m.isHidden && m.name.toLowerCase().includes(q));
        const cs = t.columns.filter((c) => !c.isHidden && c.name.toLowerCase().includes(q));
        if (q && !ms.length && !cs.length) return "";
        return `<div class="tbl"><div class="name">▾ ${escapeHtml(t.name)}</div>
          ${ms.map((m) => `<div class="fld measure" draggable="true" data-t="${escapeHtml(t.name)}" data-c="${escapeHtml(m.name)}" data-m="1" title="${escapeHtml(m.expression)}"><span class="ic">fx</span>${escapeHtml(m.name)}</div>`).join("")}
          ${cs.map((c) => `<div class="fld" draggable="true" data-t="${escapeHtml(t.name)}" data-c="${escapeHtml(c.name)}" data-ty="${escapeHtml(c.type || "")}"><span class="ic">${NUMERIC.test(c.type || "") && !KEYLIKE.test(c.name) ? "Σ" : ""}</span>${escapeHtml(c.name)}</div>`).join("")}
        </div>`;
      }).join("");
      list.querySelectorAll(".tbl > .name").forEach((n) => n.addEventListener("click", () => {
        const hide = !n.dataset.c;
        n.dataset.c = hide ? "1" : "";
        n.textContent = `${hide ? "▸" : "▾"} ${n.textContent.slice(2)}`;
        n.parentElement.querySelectorAll(".fld").forEach((f) => f.classList.toggle("hidden", hide));
      }));
      list.querySelectorAll(".fld").forEach((f) => {
        f.addEventListener("click", () => this.addField(f.dataset.t, f.dataset.c, !!f.dataset.m, f.dataset.ty));
        f.addEventListener("dragstart", (e) => e.dataTransfer.setData("text/openbi-field", JSON.stringify({ t: f.dataset.t, c: f.dataset.c, m: !!f.dataset.m, ty: f.dataset.ty })));
      });
    };
    draw();
    body.querySelector(".search").addEventListener("input", (e) => draw(e.target.value.toLowerCase()));
  }

  renderVizPane() {
    const body = this.root.querySelector(".pane.viz .pane-body");
    if (!this.editing) return;
    const v = this.selected && this.visualByName(this.selected);
    const types = Object.entries(VISUALS).map(([k, d]) => `<button title="${escapeHtml(d.label)}" data-type="${k}" class="${v && v.type === k ? "on" : ""}">${d.icon}</button>`).join("");
    let html = `<div class="vtypes">${types}</div>`;
    if (!v) {
      html += `<div class="muted" style="font-size:12.5px">Pick a visual type to add it to the page, or select a visual on the canvas. Click or drag fields from the Data pane into the wells.</div>`;
    } else {
      html += `<div class="section-title">${escapeHtml(VISUALS[v.type].label)}</div>`;
      for (const r of VISUALS[v.type].roles) {
        const list = v.fields?.[r.name] || [];
        html += `<label>${escapeHtml(r.label)}</label><div class="well ${this.activeWell === r.name ? "target" : ""}" data-role="${r.name}">
          ${list.map((ref, i) => {
            const p = parseRef(ref);
            const agg = p.kind === "implicit" ? `<select data-agg="${i}">${AGGREGATES.map(([a, l]) => `<option value="${a}" ${a === p.aggregate ? "selected" : ""}>${l}</option>`).join("")}</select>` : "";
            return `<div class="chip"><span>${escapeHtml(p.kind === "implicit" ? p.column : p.label)}</span>${agg}<span class="x" data-rm="${i}">×</span></div>`;
          }).join("") || `<div class="dropzone-hint">Add data fields here</div>`}</div>`;
      }
      const f = v.format || {};
      html += `<div class="section-title">Format</div>
        <label>Title</label><input data-f="title" style="width:100%" value="${escapeHtml(v.title ?? "")}" placeholder="${escapeHtml(this.autoTitle(v))}">`;
      if (["columnChart", "stackedColumnChart", "barChart", "stackedBarChart", "lineChart", "areaChart", "comboChart", "pieChart", "donutChart", "treemap", "funnel"].includes(v.type)) {
        html += `<label>Sort</label><select data-f="sort" style="width:100%">
            <option value="">Default</option><option value="valueDesc" ${f.sort === "valueDesc" ? "selected" : ""}>Value, descending</option>
            <option value="valueAsc" ${f.sort === "valueAsc" ? "selected" : ""}>Value, ascending</option><option value="category" ${f.sort === "category" ? "selected" : ""}>Category</option></select>
          <label>Show top N (blank = all)</label><input data-f="topN" type="number" min="1" style="width:100%" value="${escapeHtml(f.topN ?? "")}">
          <label class="row" style="color:inherit"><input type="checkbox" data-f="showLegend" ${f.showLegend === false ? "" : "checked"}> Legend</label>
          <label class="row" style="color:inherit"><input type="checkbox" data-f="dataLabels" ${f.dataLabels ? "checked" : ""}> Data labels</label>`;
      }
      if (v.type === "slicer") html += `<label>Style</label><select data-f="style" style="width:100%"><option value="list">List</option><option value="dropdown" ${f.style === "dropdown" ? "selected" : ""}>Dropdown</option></select>`;
      if (v.type === "textbox") html += `<label>Text</label><textarea data-f="text" rows="5" style="width:100%;font-family:inherit">${escapeHtml(f.text || "")}</textarea>`;
      if (v.type === "card") html += `<label>Value color</label><input data-f="color" type="color" value="${escapeHtml(f.color || "#1f2328")}">`;
      html += `<div class="row" style="margin-top:12px"><button data-act="dup">Duplicate</button><button class="danger" data-act="del">Delete</button></div>`;
    }
    body.innerHTML = html;
    body.querySelectorAll("[data-type]").forEach((b) => b.addEventListener("click", () => (v ? this.changeType(v, b.dataset.type) : this.newVisual(b.dataset.type))));
    if (!v) return;
    body.querySelectorAll(".well").forEach((w) => {
      w.addEventListener("click", (e) => {
        if (e.target.dataset.rm !== undefined || e.target.tagName === "SELECT") return;
        this.activeWell = this.activeWell === w.dataset.role ? null : w.dataset.role;
        this.renderVizPane();
      });
      w.addEventListener("dragover", (e) => { e.preventDefault(); w.classList.add("target"); });
      w.addEventListener("dragleave", () => w.classList.toggle("target", this.activeWell === w.dataset.role));
      w.addEventListener("drop", (e) => {
        e.preventDefault();
        const d = JSON.parse(e.dataTransfer.getData("text/openbi-field") || "null");
        if (!d) return;
        const prev = this.activeWell;
        this.activeWell = w.dataset.role;
        this.addField(d.t, d.c, d.m, d.ty);
        this.activeWell = prev;
        this.renderVizPane();
      });
      w.querySelectorAll("[data-rm]").forEach((x) => x.addEventListener("click", () => {
        v.fields[w.dataset.role].splice(Number(x.dataset.rm), 1);
        this.markDirty(); this.renderVizPane(); this.refreshVisual(v);
      }));
      w.querySelectorAll("[data-agg]").forEach((s) => s.addEventListener("change", () => {
        const ref = v.fields[w.dataset.role][Number(s.dataset.agg)];
        ref.aggregate = s.value;
        delete ref.name;
        this.markDirty(); this.refreshVisual(v);
      }));
    });
    body.querySelectorAll("[data-f]").forEach((inp) => inp.addEventListener(inp.type === "checkbox" || inp.tagName === "SELECT" || inp.type === "color" ? "change" : "input", () => {
      const k = inp.dataset.f;
      if (k === "title") v.title = inp.value === "" ? undefined : inp.value;
      else {
        v.format = v.format || {};
        v.format[k] = inp.type === "checkbox" ? inp.checked : inp.value === "" ? undefined : inp.value;
      }
      this.markDirty();
      clearTimeout(this._fmtT);
      this._fmtT = setTimeout(() => this.refreshVisual(v), 250);
    }));
    body.querySelector("[data-act=dup]").addEventListener("click", () => this.duplicate(v));
    body.querySelector("[data-act=del]").addEventListener("click", () => this.deleteVisual(v.name));
  }

  renderFilterPane() {
    const body = this.root.querySelector(".pane.filters .pane-body");
    const v = this.selected && this.visualByName(this.selected);
    const section = (title, list, scope) => {
      const cards = list.map((f, i) => this.filterCard(f, scope, i)).join("");
      const add = this.editing ? `<button data-add="${scope}" style="width:100%;margin-bottom:4px">+ Add filter</button>` : "";
      return `<div class="section-title">${title}</div>${cards || '<div class="muted" style="font-size:12px;margin-bottom:6px">None</div>'}${add}`;
    };
    let html = "";
    if (v) html += section("Filters on this visual", v.filters || [], "visual");
    html += section("Filters on this page", this.page.filters || [], "page");
    html += section("Filters on all pages", this.report.filters, "report");
    body.innerHTML = html;
    body.querySelectorAll("[data-add]").forEach((b) => b.addEventListener("click", () => this.addFilterDialog(b.dataset.add)));
    body.querySelectorAll(".filter-card").forEach((card) => this.bindFilterCard(card));
  }

  filterList(scope) {
    if (scope === "report") return this.report.filters;
    if (scope === "page") return (this.page.filters = this.page.filters || []);
    const v = this.visualByName(this.selected);
    return (v.filters = v.filters || []);
  }

  filterCard(f, scope, i) {
    return `<div class="filter-card" data-scope="${scope}" data-i="${i}">
      <div class="fhead"><span class="grow">${escapeHtml(filterTargetLabel(f))}</span>${this.editing ? '<button class="icon" data-x title="Remove">×</button>' : ""}</div>
      <div class="muted">${escapeHtml(describeFilter(f))}</div><div class="vals"></div></div>`;
  }

  async bindFilterCard(card) {
    const list = this.filterList(card.dataset.scope);
    const f = list[Number(card.dataset.i)];
    const apply = () => { this.markDirty(); card.querySelector(".muted").textContent = describeFilter(f); this.refreshAll(); };
    card.querySelector("[data-x]")?.addEventListener("click", () => {
      list.splice(Number(card.dataset.i), 1);
      this.markDirty(); this.renderFilterPane(); this.refreshAll();
    });
    const box = card.querySelector(".vals");
    const ft = f.filterType ?? 1;
    if (ft === 1 && f.target?.column) {
      const key = `${f.target.table}|${f.target.column}`;
      let values = this.valueCache.get(key);
      if (!values) {
        try {
          const res = await this.query({ select: [selectItem(columnRef(f.target.table, f.target.column))], top: 500 }, []);
          values = res.rows.map((r) => r[0]);
          this.valueCache.set(key, values);
        } catch (e) { box.textContent = e.message; return; }
      }
      const chosen = new Set((f.values || []).map((x) => JSON.stringify(x)));
      box.innerHTML = values.map((x, i) => `<label><input type="checkbox" data-v="${i}" ${chosen.has(JSON.stringify(x)) ? "checked" : ""}>${escapeHtml(x === null ? "(Blank)" : x)}</label>`).join("");
      box.querySelectorAll("input").forEach((cb) => cb.addEventListener("change", () => {
        f.values = [...box.querySelectorAll("input:checked")].map((x) => values[Number(x.dataset.v)]);
        f.operator = f.values.length ? (f.operator === "NotIn" ? "NotIn" : "In") : "All";
        apply();
      }));
    } else if (ft === 0) {
      const c = f.conditions?.[0] || { operator: "GreaterThan", value: "" };
      const ops = ["Is", "IsNot", "LessThan", "LessThanOrEqual", "GreaterThan", "GreaterThanOrEqual", "Contains", "DoesNotContain", "StartsWith", "IsBlank", "IsNotBlank"];
      box.innerHTML = `<div class="row"><select>${ops.map((o) => `<option ${o === c.operator ? "selected" : ""}>${o}</option>`).join("")}</select><input style="width:80px" value="${escapeHtml(c.value ?? "")}"></div>`;
      const [sel, inp] = box.querySelectorAll("select, input");
      const upd = () => {
        const raw = inp.value;
        const value = raw !== "" && !Number.isNaN(Number(raw)) ? Number(raw) : raw;
        f.conditions = [{ operator: sel.value, value }];
        f.logicalOperator = "And";
        apply();
      };
      sel.addEventListener("change", upd);
      inp.addEventListener("change", upd);
    } else if (ft === 5) {
      box.innerHTML = `<div class="row"><select><option ${f.operator === "Top" ? "selected" : ""}>Top</option><option ${f.operator === "Bottom" ? "selected" : ""}>Bottom</option></select><input type="number" min="1" style="width:60px" value="${f.itemCount}"></div>`;
      const [sel, inp] = box.querySelectorAll("select, input");
      const upd = () => { f.operator = sel.value; f.itemCount = Number(inp.value) || 1; apply(); };
      sel.addEventListener("change", upd);
      inp.addEventListener("change", upd);
    }
  }

  addFilterDialog(scope) {
    const cols = this.schema.tables.flatMap((t) => t.columns.filter((c) => !c.isHidden).map((c) => [t.name, c.name]));
    const measures = this.schema.tables.flatMap((t) => t.measures.map((m) => m.name));
    const m = modal("Add filter", `
      <label>Field</label><select id="ff" style="width:100%">${cols.map(([t, c], i) => `<option value="${i}">${escapeHtml(t)}[${escapeHtml(c)}]</option>`).join("")}</select>
      <label>Filter type</label><select id="ft" style="width:100%"><option value="1">Basic (list)</option><option value="0">Advanced (condition)</option><option value="5">Top N</option></select>
      <div id="tn" class="hidden"><label>By measure</label><select id="tm" style="width:100%">${measures.map((x) => `<option>${escapeHtml(x)}</option>`).join("")}</select></div>
      <div class="row" style="margin-top:14px"><span class="grow"></span><button class="primary" id="fa">Add</button></div>`);
    m.querySelector("#ft").addEventListener("change", (e) => m.querySelector("#tn").classList.toggle("hidden", e.target.value !== "5"));
    m.querySelector("#fa").addEventListener("click", () => {
      const [t, c] = cols[Number(m.querySelector("#ff").value)];
      const ft = Number(m.querySelector("#ft").value);
      const f = ft === 1 ? basicFilter(t, c, [], "All") : ft === 0 ? advancedFilter(t, c, "And", [{ operator: "Is", value: "" }]) : topNFilter(t, c, "Top", 5, m.querySelector("#tm").value);
      if (ft === 0) f.conditions = [];
      this.filterList(scope).push(f);
      m.close();
      this.markDirty();
      this.renderFilterPane();
      this.refreshAll();
    });
  }

  async embedDialog() {
    const roles = this.schema.roles;
    const m = modal("Embed this report", `
      <p class="muted">Generates a short-lived embed token for this report, with an effective identity for row-level security (like Power BI's GenerateToken).</p>
      ${roles.length ? `<label>RLS roles</label><select id="er" multiple style="width:100%">${roles.map((r) => `<option>${escapeHtml(r)}</option>`).join("")}</select>
      <label>Username (USERPRINCIPALNAME)</label><input id="eu" style="width:100%" placeholder="user@example.com">` : ""}
      <label>Lifetime (minutes)</label><input id="el" type="number" value="60" min="1" max="1440">
      <div class="row" style="margin-top:12px"><span class="grow"></span><button class="primary" id="eg">Generate</button></div>
      <div id="eo"></div>`);
    m.querySelector("#eg").addEventListener("click", async () => {
      try {
        const sel = m.querySelector("#er");
        const res = await post(`/api/reports/${this.reportId}/embed-token`, {
          roles: sel ? [...sel.selectedOptions].map((o) => o.value) : [],
          username: m.querySelector("#eu")?.value || undefined,
          lifetimeMinutes: Number(m.querySelector("#el").value) || 60,
        });
        const origin = location.origin;
        const snippet = `<div id="report" style="height:600px"></div>
<script src="${origin}/static/vendor/penpal.min.js"></script>
<script src="${origin}/static/openbi-embed.js"></script>
<script>
  const report = openbi.embed(document.getElementById("report"), {
    embedUrl: "${origin}${res.embedUrl}",
    accessToken: "${res.token}",
  });
  report.on("loaded", () => console.log("loaded"));
  report.on("dataSelected", (e) => console.log(e));
</script>`;
        m.querySelector("#eo").innerHTML = `<label>Embed snippet</label><textarea rows="12" style="width:100%">${escapeHtml(snippet)}</textarea>
          <p class="muted">See <a href="/static/embed-example.html" target="_blank">embed-example.html</a> for the full JavaScript API.</p>`;
      } catch (e) { m.querySelector("#eo").innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`; }
    });
  }

  // ------------------------------------------------- embedding API --
  getFilters() { return [...this.report.filters, ...this.extraFilters]; }

  setFilters(filters) {
    this.extraFilters = filters || [];
    this.refreshAll();
    this.onEvent("filtersApplied", { filters: this.extraFilters });
  }

  getSlicerState() { return { ...this.slicers }; }

  setSlicerState(name, values) { this.slicers[name] = values; this.refreshAll(); }
}
