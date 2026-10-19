// Dashboards: one canvas of tiles pinned from visuals of several reports.
import { get, post, put } from "./api.js";
import { escapeHtml } from "./format.js";
import { toast } from "./ui.js";
import { DEFAULT_THEME, buildQueries, disposeChart, renderVisual } from "./visuals.js";
import { columnRef, measureRef } from "./fields.js";

/* global GridStack */

export class DashboardView {
  constructor(root, id) { this.root = root; this.id = id; this.editing = false; }

  async load() {
    this.dash = await get(`/api/dashboards/${this.id}`);
    this.root.innerHTML = `<div class="report"><div class="canvas-wrap">
      <div class="toolbar"><span class="title">${escapeHtml(this.dash.name)}</span>
        ${this.dash.canEdit ? '<button data-act="edit">Edit layout</button><button data-act="save" class="primary hidden">Save</button>' : ""}
        <span class="grow"></span><a href="#/ws/${this.dash.workspaceId}">Back to workspace</a></div>
      <div class="canvas"><div class="grid-stack"></div></div></div></div>`;
    this.grid = GridStack.init({ column: 12, cellHeight: 70, margin: 5, float: true, staticGrid: true, minRow: 6 }, this.root.querySelector(".grid-stack"));
    this.root.querySelector("[data-act=edit]")?.addEventListener("click", (e) => {
      this.editing = !this.editing;
      this.grid.setStatic(!this.editing);
      e.target.textContent = this.editing ? "Done" : "Edit layout";
      this.root.querySelector("[data-act=save]").classList.toggle("hidden", !this.editing);
    });
    this.root.querySelector("[data-act=save]")?.addEventListener("click", () => this.save());
    const reports = new Map();
    const schemas = new Map();
    for (const [i, tile] of this.dash.tiles.entries()) {
      const el = this.grid.addWidget({ x: tile.x ?? 0, y: tile.y ?? 0, w: tile.w ?? 4, h: tile.h ?? 3, id: String(i) });
      const c = el.querySelector(".grid-stack-item-content");
      c.innerHTML = `<div class="visual"><div class="vhead"><div class="vtitle"></div><div class="vmenu"><button data-rm title="Remove tile" class="${this.dash.canEdit ? "" : "hidden"}">×</button></div></div><div class="vbody"><div class="vmsg">Loading…</div></div></div>`;
      c.querySelector("[data-rm]").addEventListener("click", () => { this.grid.removeWidget(el); el._removed = true; this.editing = true; this.root.querySelector("[data-act=save]").classList.remove("hidden"); });
      c.querySelector(".vtitle").addEventListener("click", () => { location.hash = `#/report/${tile.reportId}?page=${encodeURIComponent(tile.page)}`; });
      c.querySelector(".vtitle").style.cursor = "pointer";
      try {
        if (!reports.has(tile.reportId)) reports.set(tile.reportId, get(`/api/reports/${tile.reportId}`));
        const rep = await reports.get(tile.reportId);
        if (!schemas.has(rep.datasetId)) schemas.set(rep.datasetId, get(`/api/datasets/${rep.datasetId}/schema`));
        const schema = await schemas.get(rep.datasetId);
        const page = rep.definition.pages.find((p) => p.name === tile.page);
        const v = page?.visuals.find((x) => x.name === tile.visual);
        if (!v) throw new Error("visual no longer exists");
        c.querySelector(".vtitle").textContent = v.title || `${rep.name} · ${page.displayName || page.name}`;
        const filters = [...(rep.definition.filters || []), ...(page.filters || []), ...(v.filters || [])];
        const formats = new Map();
        for (const t of schema.tables) {
          for (const m of t.measures) formats.set(measureRef(m.name), m.formatString);
          for (const col of t.columns) if (col.formatString) formats.set(columnRef(t.name, col.name), col.formatString);
        }
        const queries = buildQueries(v);
        const body = c.querySelector(".vbody");
        if (!queries) { renderVisual(body, v, {}, {}); continue; }
        const entries = await Promise.all(Object.entries(queries).map(async ([k, q]) => [k, await post(`/api/datasets/${rep.datasetId}/query`, { ...q, filters })]));
        disposeChart(body);
        renderVisual(body, v, Object.fromEntries(entries), {
          theme: { ...DEFAULT_THEME, ...(rep.definition.theme || {}) },
          formatOf: (ref) => formats.get(typeof ref === "string" ? ref : ref.field) || (typeof ref === "string" && !ref.startsWith("[") ? "General" : undefined),
          selectedKey: null, slicerValues: [], onSelect: () => {}, onSlicer: () => {},
        });
      } catch (e) {
        c.querySelector(".vbody").innerHTML = `<div class="vmsg err">${escapeHtml(e.message)}</div>`;
      }
    }
  }

  async save() {
    const tiles = [];
    for (const el of this.grid.getGridItems()) {
      const n = el.gridstackNode;
      const t = this.dash.tiles[Number(n.id)];
      if (!t) continue;
      const { datasetId, ...rest } = t;
      tiles.push({ ...rest, x: n.x, y: n.y, w: n.w, h: n.h });
    }
    await put(`/api/dashboards/${this.id}`, { name: this.dash.name, tiles });
    toast("Dashboard saved");
    this.load();
  }

  destroy() {
    this.root.querySelectorAll(".vbody").forEach(disposeChart);
    this.grid?.destroy(false);
  }
}

export async function createDashboard(workspaceId, name) {
  return post(`/api/workspaces/${workspaceId}/dashboards`, { name, tiles: [] });
}
