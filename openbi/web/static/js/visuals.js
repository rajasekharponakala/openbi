// Visual types. Each declares its field wells (data roles, like a Power BI
// visual's capabilities.json), how to build its semantic query, and how to
// render the returned rows (charts are drawn with Apache ECharts).
import { basicFilter, outputName, parseRef, selectItem } from "./fields.js";
import { compactNumber, escapeHtml, formatValue, isNumber } from "./format.js";

/* global echarts */

export const DEFAULT_THEME = {
  name: "OpenBI",
  dataColors: ["#2E6FDB", "#F28C28", "#2BA84A", "#9B59B6", "#E04F5F", "#17A2B8", "#C9A227", "#6C757D", "#8E44AD", "#16A085"],
  background: "#ffffff",
  foreground: "#1f2328",
};

const C = (name, label, max = 1) => ({ name, label, accepts: "column", max });
const M = (name, label, max = 1) => ({ name, label, accepts: "value", max });
const A = (name, label, max = 50) => ({ name, label, accepts: "any", max });

export const VISUALS = {
  card: { label: "Card", icon: "▭", roles: [M("values", "Fields")] },
  columnChart: { label: "Clustered column", icon: "▥", roles: [C("category", "X-axis"), C("series", "Legend"), M("values", "Y-axis", 5)] },
  stackedColumnChart: { label: "Stacked column", icon: "▤", roles: [C("category", "X-axis"), C("series", "Legend"), M("values", "Y-axis", 5)] },
  barChart: { label: "Clustered bar", icon: "☰", roles: [C("category", "Y-axis"), C("series", "Legend"), M("values", "X-axis", 5)] },
  stackedBarChart: { label: "Stacked bar", icon: "≣", roles: [C("category", "Y-axis"), C("series", "Legend"), M("values", "X-axis", 5)] },
  lineChart: { label: "Line", icon: "⟋", roles: [C("category", "X-axis"), C("series", "Legend"), M("values", "Y-axis", 5)] },
  areaChart: { label: "Area", icon: "◿", roles: [C("category", "X-axis"), C("series", "Legend"), M("values", "Y-axis", 5)] },
  comboChart: { label: "Line and column", icon: "⫶", roles: [C("category", "X-axis"), M("values", "Column y-axis", 5), M("lineValues", "Line y-axis", 5)] },
  pieChart: { label: "Pie", icon: "◔", roles: [C("category", "Legend"), M("values", "Values")] },
  donutChart: { label: "Donut", icon: "◎", roles: [C("category", "Legend"), M("values", "Values")] },
  treemap: { label: "Treemap", icon: "▦", roles: [C("category", "Category"), M("values", "Values")] },
  funnel: { label: "Funnel", icon: "⏷", roles: [C("category", "Category"), M("values", "Values")] },
  scatterChart: { label: "Scatter", icon: "⁘", roles: [C("category", "Values"), M("x", "X-axis"), M("y", "Y-axis"), M("size", "Size")] },
  gauge: { label: "Gauge", icon: "◠", roles: [M("values", "Value"), M("target", "Target value")] },
  table: { label: "Table", icon: "▦", roles: [A("columns", "Columns")] },
  matrix: { label: "Matrix", icon: "⊞", roles: [C("rows", "Rows", 3), C("columns", "Columns"), M("values", "Values", 10)] },
  slicer: { label: "Slicer", icon: "⧩", roles: [C("field", "Field")] },
  textbox: { label: "Text box", icon: "T", roles: [] },
};

const CARTESIAN = new Set(["columnChart", "stackedColumnChart", "barChart", "stackedBarChart", "lineChart", "areaChart", "comboChart"]);

export function wells(visual) { return visual.fields || {}; }

function refs(visual, role) { return (wells(visual)[role] || []); }

/** Build the semantic queries a visual needs; null when wells are incomplete. */
export function buildQueries(visual) {
  const t = visual.type;
  const f = (r) => refs(visual, r);
  const sel = (list) => list.map(selectItem);
  const fmt = visual.format || {};
  if (t === "textbox") return null;
  if (t === "card") return f("values").length ? { main: { select: sel(f("values").slice(0, 1)) } } : null;
  if (t === "gauge") return f("values").length ? { main: { select: sel([...f("values"), ...f("target")]) } } : null;
  if (t === "slicer") {
    if (!f("field").length) return null;
    return { main: { select: sel(f("field")), top: 1000 } };
  }
  if (t === "table") {
    const cols = f("columns");
    if (!cols.length) return null;
    const q = { main: { select: sel(cols), top: 2000 } };
    const values = cols.filter((r) => parseRef(r).kind !== "column");
    if (values.length && values.length < cols.length) q.total = { select: sel(values) };
    if (fmt.sortBy) q.main.orderBy = [{ field: fmt.sortBy, direction: fmt.sortDir || "desc" }];
    return q;
  }
  if (t === "matrix") {
    const rows = f("rows"), cols = f("columns"), vals = f("values");
    if (!vals.length || (!rows.length && !cols.length)) return null;
    const q = { main: { select: sel([...rows, ...cols, ...vals]), top: 5000 }, grand: { select: sel(vals) } };
    if (rows.length && cols.length) {
      q.rowTotals = { select: sel([...rows, ...vals]), top: 5000 };
      q.colTotals = { select: sel([...cols, ...vals]), top: 5000 };
    }
    return q;
  }
  if (t === "scatterChart") {
    if (!f("category").length || !f("x").length || !f("y").length) return null;
    return { main: { select: sel([...f("category"), ...f("x"), ...f("y"), ...f("size")]), top: 2000 } };
  }
  // category charts
  const cat = f("category"), series = f("series") || [], vals = [...f("values"), ...(t === "comboChart" ? f("lineValues") : [])];
  if (!cat.length || !vals.length) return null;
  const q = { select: sel([...cat, ...(CARTESIAN.has(t) ? series : []), ...vals]), top: 5000 };
  if (fmt.sort === "valueDesc" || (!fmt.sort && ["barChart", "pieChart", "donutChart", "treemap", "funnel"].includes(t) && !series.length)) {
    q.orderBy = [{ field: outputName(vals[0]), direction: "desc" }];
  } else if (fmt.sort === "valueAsc") {
    q.orderBy = [{ field: outputName(vals[0]), direction: "asc" }];
  } else if (fmt.sort === "category") {
    q.orderBy = [{ field: outputName(cat[0]), direction: "asc" }];
  }
  if (fmt.topN) q.top = Number(fmt.topN);
  return { main: q };
}

// ------------------------------------------------------------- render --
export function renderVisual(el, visual, data, ctx) {
  const t = visual.type;
  disposeChart(el);
  if (t === "textbox") return renderText(el, visual);
  if (t === "card") return renderCard(el, visual, data, ctx);
  if (t === "table") return renderTable(el, visual, data, ctx);
  if (t === "matrix") return renderMatrix(el, visual, data, ctx);
  if (t === "slicer") return renderSlicer(el, visual, data, ctx);
  return renderChart(el, visual, data, ctx);
}

export function disposeChart(el) {
  const inst = window.echarts && echarts.getInstanceByDom(el);
  if (inst) inst.dispose();
  if (el._ro) { el._ro.disconnect(); el._ro = null; }
}

function colIndex(data, ref) {
  const name = outputName(ref);
  return data.columns.findIndex((c) => c.name === name);
}

function renderText(el, visual) {
  el.innerHTML = `<div class="vtext">${escapeHtml(visual.format?.text || "")}</div>`;
}

function renderCard(el, visual, data, ctx) {
  const ref = refs(visual, "values")[0];
  const v = data.main.rows[0]?.[0];
  const fmt = ctx.formatOf(ref);
  const display = isNumber(v) && Math.abs(v) >= 10000 && !String(fmt || "").includes("%") ? formatValue(v, fmt, { compact: true }) : formatValue(v, fmt);
  el.innerHTML = `<div class="vcard" style="--card-color:${escapeHtml(visual.format?.color || ctx.theme.foreground)}">
    <div class="num">${display === "" ? "(Blank)" : escapeHtml(display)}</div>
    <div class="lbl">${escapeHtml(parseRef(ref).label)}</div></div>`;
}

function renderTable(el, visual, data, ctx) {
  const cols = refs(visual, "columns");
  const { rows } = data.main;
  const idx = cols.map((r) => colIndex(data.main, r));
  const numeric = cols.map((r) => parseRef(r).kind !== "column");
  const keyCols = cols.map((r, i) => [parseRef(r), i]).filter(([p]) => p.kind === "column");
  const sort = el._sort || null;
  let ordered = rows;
  if (sort) {
    ordered = [...rows].sort((a, b) => {
      const x = a[idx[sort.i]], y = b[idx[sort.i]];
      if (x === y) return 0;
      if (x === null) return 1;
      if (y === null) return -1;
      return (x < y ? -1 : 1) * (sort.desc ? -1 : 1);
    });
  }
  const sel = ctx.selectedKey;
  const head = cols.map((r, i) => `<th data-i="${i}" class="${numeric[i] ? "num" : ""}">${escapeHtml(parseRef(r).label)}${sort && sort.i === i ? (sort.desc ? " ▾" : " ▴") : ""}</th>`).join("");
  const body = ordered.map((row, ri) => {
    const key = JSON.stringify(keyCols.map(([, i]) => row[idx[i]]));
    const cls = sel ? (sel === key ? "sel" : "dim") : "";
    return `<tr data-r="${rows.indexOf(row)}" class="${cls}">${cols.map((r, i) => `<td class="${numeric[i] ? "num" : ""}">${escapeHtml(formatValue(row[idx[i]], ctx.formatOf(r)))}</td>`).join("")}</tr>`;
  }).join("");
  let total = "";
  if (data.total) {
    const trow = data.total.rows[0] || [];
    let vi = 0;
    total = `<tr class="total">${cols.map((r, i) => {
      if (!numeric[i]) return `<td>${i === 0 ? "Total" : ""}</td>`;
      const v = trow[vi++];
      return `<td class="num">${escapeHtml(formatValue(v, ctx.formatOf(r)))}</td>`;
    }).join("")}</tr>`;
  }
  el.innerHTML = `<div class="vtable"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody><tfoot>${total}</tfoot></table></div>`;
  el.querySelectorAll("th").forEach((th) => th.addEventListener("click", () => {
    const i = Number(th.dataset.i);
    el._sort = { i, desc: el._sort && el._sort.i === i ? !el._sort.desc : numeric[i] };
    renderTable(el, visual, data, ctx);
  }));
  if (keyCols.length) {
    el.querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", () => {
      const row = rows[Number(tr.dataset.r)];
      const key = JSON.stringify(keyCols.map(([, i]) => row[idx[i]]));
      if (sel === key) return ctx.onSelect(null);
      ctx.onSelect({ key, filters: keyCols.map(([p, i]) => basicFilter(p.table, p.column, [row[idx[i]]])) });
    }));
  }
}

function renderMatrix(el, visual, data, ctx) {
  const rowRefs = refs(visual, "rows"), colRefs = refs(visual, "columns"), valRefs = refs(visual, "values");
  const m = data.main;
  const ri = rowRefs.map((r) => colIndex(m, r));
  const ci = colRefs.map((r) => colIndex(m, r));
  const vi = valRefs.map((r) => colIndex(m, r));
  const rowKeys = [], colKeys = [], cells = new Map();
  const seenR = new Set(), seenC = new Set();
  for (const row of m.rows) {
    const rk = JSON.stringify(ri.map((i) => row[i]));
    const ck = JSON.stringify(ci.map((i) => row[i]));
    if (!seenR.has(rk)) { seenR.add(rk); rowKeys.push(rk); }
    if (!seenC.has(ck)) { seenC.add(ck); colKeys.push(ck); }
    cells.set(rk + "|" + ck, vi.map((i) => row[i]));
  }
  colKeys.sort();
  const lookup = (q, keyIdx) => {
    const map = new Map();
    if (!q) return map;
    const n = keyIdx;
    for (const row of q.rows) map.set(JSON.stringify(row.slice(0, n)), row.slice(n));
    return map;
  };
  const rowTotals = lookup(data.rowTotals, rowRefs.length);
  const colTotals = lookup(data.colTotals, colRefs.length);
  const grand = data.grand?.rows[0] || [];
  const fmt = valRefs.map((r) => ctx.formatOf(r));
  const multiV = valRefs.length > 1;
  const hasCols = colRefs.length > 0;
  let head1 = `<th rowspan="${multiV ? 2 : 1}">${rowRefs.map((r) => escapeHtml(parseRef(r).label)).join(" / ")}</th>`;
  let head2 = "";
  const colList = hasCols ? colKeys : [];
  for (const ck of colList) {
    const label = JSON.parse(ck).map((v) => (v === null ? "(Blank)" : v)).join(" / ");
    head1 += `<th class="num" colspan="${valRefs.length}">${escapeHtml(label)}</th>`;
    if (multiV) head2 += valRefs.map((r) => `<th class="num">${escapeHtml(parseRef(r).label)}</th>`).join("");
  }
  if (rowRefs.length) {
    head1 += `<th class="num" colspan="${valRefs.length}">${hasCols ? "Total" : valRefs.length === 1 ? escapeHtml(parseRef(valRefs[0]).label) : ""}</th>`;
    if (multiV) head2 += valRefs.map((r) => `<th class="num">${escapeHtml(parseRef(r).label)}</th>`).join("");
  }
  const td = (vals) => vals.map((v, i) => `<td class="num">${escapeHtml(formatValue(v, fmt[i]))}</td>`).join("");
  const body = (rowRefs.length ? rowKeys : ["[]"]).map((rk) => {
    const label = JSON.parse(rk).map((v) => (v === null ? "(Blank)" : v)).join(" / ");
    let tds = "";
    for (const ck of colList) tds += td(cells.get(rk + "|" + ck) || valRefs.map(() => null));
    const total = hasCols ? (rowTotals.get(rk) || valRefs.map(() => null)) : (cells.get(rk + "|[]") || []);
    return `<tr><td>${escapeHtml(label)}</td>${tds}${rowRefs.length ? td(total) : ""}</tr>`;
  }).join("");
  let foot = `<tr class="total"><td>Total</td>`;
  for (const ck of colList) foot += td(colTotals.get(ck) || (rowRefs.length ? valRefs.map(() => null) : cells.get("[]|" + ck) || []));
  if (rowRefs.length) foot += td(grand);
  foot += "</tr>";
  el.innerHTML = `<div class="vtable"><table><thead><tr>${head1}</tr>${multiV ? `<tr>${head2}</tr>` : ""}</thead><tbody>${body}</tbody><tfoot>${foot}</tfoot></table></div>`;
}

function renderSlicer(el, visual, data, ctx) {
  const ref = refs(visual, "field")[0];
  const p = parseRef(ref);
  const values = data.main.rows.map((r) => r[0]);
  const selected = new Set((ctx.slicerValues || []).map((v) => JSON.stringify(v)));
  const style = visual.format?.style || "list";
  const label = (v) => (v === null ? "(Blank)" : formatValue(v, ctx.formatOf(ref)));
  if (style === "dropdown") {
    const opts = values.map((v, i) => `<option value="${i}" ${selected.has(JSON.stringify(v)) ? "selected" : ""}>${escapeHtml(label(v))}</option>`).join("");
    el.innerHTML = `<div class="vslicer"><select><option value="">All</option>${opts}</select></div>`;
    el.querySelector("select").addEventListener("change", (e) => {
      const i = e.target.value;
      ctx.onSlicer(i === "" ? [] : [values[Number(i)]], p);
    });
    return;
  }
  const items = values.map((v, i) => `<label data-t="${escapeHtml(String(label(v)).toLowerCase())}"><input type="checkbox" data-i="${i}" ${selected.has(JSON.stringify(v)) ? "checked" : ""}>${escapeHtml(label(v))}</label>`).join("");
  el.innerHTML = `<div class="vslicer">${values.length > 8 ? '<input class="search" placeholder="Search">' : ""}<label><input type="checkbox" data-all ${selected.size === 0 ? "checked" : ""}>Select all</label>${items}</div>`;
  const search = el.querySelector(".search");
  if (search) search.addEventListener("input", () => {
    const q = search.value.toLowerCase();
    el.querySelectorAll("label[data-t]").forEach((l) => l.classList.toggle("hidden", !l.dataset.t.includes(q)));
  });
  el.querySelectorAll("input[type=checkbox]").forEach((cb) => cb.addEventListener("change", () => {
    if (cb.dataset.all !== undefined) return ctx.onSlicer([], p);
    const chosen = [...el.querySelectorAll("input[data-i]:checked")].map((x) => values[Number(x.dataset.i)]);
    ctx.onSlicer(chosen, p);
  }));
}

// -------------------------------------------------------------- charts --
function axisFormatter(fmt) {
  return (v) => (String(fmt || "").includes("%") ? formatValue(v, fmt) : compactNumber(v));
}

function renderChart(el, visual, data, ctx) {
  const t = visual.type;
  const chart = echarts.init(el, null, { renderer: "canvas" });
  const theme = ctx.theme;
  const fmt = visual.format || {};
  const m = data.main;
  const option = { color: theme.dataColors, textStyle: { fontFamily: "Segoe UI, system-ui, sans-serif" }, animationDuration: 300 };
  const tooltipFmt = (ref) => (v) => formatValue(v, ctx.formatOf(ref));
  const sel = ctx.selectedKey;
  const dim = (key) => (sel && sel !== key ? 0.35 : 1);

  if (t === "scatterChart") {
    const [cat, x, y, size] = ["category", "x", "y", "size"].map((r) => refs(visual, r)[0]);
    const ci = colIndex(m, cat), xi = colIndex(m, x), yi = colIndex(m, y), si = size ? colIndex(m, size) : -1;
    const maxS = si >= 0 ? Math.max(...m.rows.map((r) => r[si] || 0), 1) : 1;
    option.grid = { left: 50, right: 20, top: 30, bottom: 40 };
    option.xAxis = { type: "value", name: parseRef(x).label, nameLocation: "middle", nameGap: 26, axisLabel: { formatter: axisFormatter(ctx.formatOf(x)) } };
    option.yAxis = { type: "value", name: parseRef(y).label, axisLabel: { formatter: axisFormatter(ctx.formatOf(y)) } };
    option.tooltip = { formatter: (p) => `<b>${escapeHtml(p.data.name)}</b><br>${escapeHtml(parseRef(x).label)}: ${formatValue(p.data.value[0], ctx.formatOf(x))}<br>${escapeHtml(parseRef(y).label)}: ${formatValue(p.data.value[1], ctx.formatOf(y))}${si >= 0 ? `<br>${escapeHtml(parseRef(size).label)}: ${formatValue(p.data.value[2], ctx.formatOf(size))}` : ""}` };
    option.series = [{
      type: "scatter",
      data: m.rows.map((r) => ({ name: r[ci], value: [r[xi], r[yi], si >= 0 ? r[si] : 1], itemStyle: { opacity: dim(JSON.stringify([r[ci]])) * 0.85 } })),
      symbolSize: (v) => (si >= 0 ? 6 + 34 * Math.sqrt(Math.max(v[2], 0) / maxS) : 10),
    }];
    chart.setOption(option);
    const p = parseRef(cat);
    chart.on("click", (e) => clickCategory(ctx, [[p, e.data.name]]));
    observe(el, chart);
    return;
  }

  if (t === "gauge") {
    const [vref, tref] = [refs(visual, "values")[0], refs(visual, "target")[0]];
    const row = m.rows[0] || [];
    const v = row[0] ?? 0, target = tref ? row[1] : null;
    const max = Math.max(target ? target * 1.25 : 0, v * 1.1, 1);
    const f = ctx.formatOf(vref);
    option.series = [{
      type: "gauge", startAngle: 180, endAngle: 0, min: 0, max, radius: "95%", center: ["50%", "72%"],
      progress: { show: true, width: 18 }, axisLine: { lineStyle: { width: 18 } }, pointer: { show: false },
      axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false },
      anchor: { show: false }, title: { show: true, offsetCenter: [0, "18%"], fontSize: 12, color: "#646b75" },
      detail: { valueAnimation: true, offsetCenter: [0, "-12%"], fontSize: 22, fontWeight: 600, formatter: (x) => formatValue(x, f, { compact: true }) },
      data: [{ value: v, name: target ? `Target ${formatValue(target, ctx.formatOf(tref), { compact: true })}` : parseRef(vref).label }],
      markLine: undefined,
    }];
    chart.setOption(option);
    observe(el, chart);
    return;
  }

  const catRef = refs(visual, "category")[0];
  const catP = parseRef(catRef);
  const ci = colIndex(m, catRef);
  const valRefs = refs(visual, "values");
  const lineRefs = t === "comboChart" ? refs(visual, "lineValues") : [];
  const seriesRef = CARTESIAN.has(t) && t !== "comboChart" ? refs(visual, "series")[0] : null;
  const catLabel = (v) => (v === null ? "(Blank)" : String(v));

  if (["pieChart", "donutChart", "treemap", "funnel"].includes(t)) {
    const vref = valRefs[0], vi = colIndex(m, vref);
    const items = m.rows.map((r) => ({ name: catLabel(r[ci]), value: r[vi], raw: r[ci], itemStyle: { opacity: dim(JSON.stringify([r[ci]])) } }));
    option.tooltip = { trigger: "item", formatter: (p) => `${escapeHtml(p.name)}<br><b>${formatValue(p.value, ctx.formatOf(vref))}</b>${p.percent !== undefined ? ` (${p.percent}%)` : ""}` };
    if (t === "treemap") {
      option.series = [{ type: "treemap", roam: false, nodeClick: false, breadcrumb: { show: false }, data: items, label: { formatter: (p) => `${p.name}\n${formatValue(p.value, ctx.formatOf(vref), { compact: true })}` }, levels: [{ colorMappingBy: "index" }] }];
      option.series[0].data.forEach((d, i) => { d.itemStyle = { ...d.itemStyle, color: theme.dataColors[i % theme.dataColors.length] }; });
    } else if (t === "funnel") {
      option.series = [{ type: "funnel", left: "4%", width: "92%", sort: "descending", gap: 2, data: items, label: { position: "inside", color: "#fff", formatter: (p) => `${p.name}: ${formatValue(p.value, ctx.formatOf(vref), { compact: true })}` } }];
    } else {
      const narrow = el.clientWidth < 420;
      const legend = fmt.showLegend !== false;
      option.legend = !legend ? undefined : narrow ? { type: "scroll", bottom: 0 } : { type: "scroll", orient: "vertical", right: 0, top: "middle" };
      option.series = [{
        type: "pie", radius: t === "donutChart" ? ["42%", "66%"] : "66%", center: [legend && !narrow ? "40%" : "50%", legend && narrow ? "45%" : "52%"],
        data: items, label: { show: fmt.dataLabels !== false, formatter: "{d}%" },
      }];
    }
    chart.setOption(option);
    chart.on("click", (e) => clickCategory(ctx, [[catP, e.data.raw]]));
    observe(el, chart);
    return;
  }

  // cartesian
  const horizontal = t === "barChart" || t === "stackedBarChart";
  const stacked = t === "stackedColumnChart" || t === "stackedBarChart";
  const categories = [];
  const catSeen = new Map();
  for (const r of m.rows) {
    const k = JSON.stringify(r[ci]);
    if (!catSeen.has(k)) { catSeen.set(k, categories.length); categories.push(r[ci]); }
  }
  const series = [];
  const kind = t === "lineChart" || t === "areaChart" ? "line" : "bar";
  if (seriesRef) {
    const si = colIndex(m, seriesRef), vref = valRefs[0], vi = colIndex(m, vref);
    const groups = new Map();
    for (const r of m.rows) {
      const g = catLabel(r[si]);
      if (!groups.has(g)) groups.set(g, { raw: r[si], data: new Array(categories.length).fill(null) });
      groups.get(g).data[catSeen.get(JSON.stringify(r[ci]))] = r[vi];
    }
    const seriesP = parseRef(seriesRef);
    for (const [name, g] of [...groups.entries()].sort((a, b) => String(a[0]).localeCompare(String(b[0])))) {
      series.push({
        name, type: kind, stack: stacked ? "s" : undefined, areaStyle: t === "areaChart" ? { opacity: 0.25 } : undefined,
        data: g.data.map((v, i) => ({ value: v, itemStyle: { opacity: dim(JSON.stringify([categories[i], g.raw])) } })),
        _series: [seriesP, g.raw], _ref: vref, emphasis: { focus: "series" }, smooth: kind === "line" ? 0.2 : undefined,
      });
    }
  } else {
    valRefs.forEach((vref) => {
      const vi = colIndex(m, vref);
      const d = new Array(categories.length).fill(null);
      for (const r of m.rows) d[catSeen.get(JSON.stringify(r[ci]))] = r[vi];
      series.push({
        name: parseRef(vref).label, type: kind, stack: stacked ? "s" : undefined, areaStyle: t === "areaChart" ? { opacity: 0.25 } : undefined,
        data: d.map((v, i) => ({ value: v, itemStyle: { opacity: dim(JSON.stringify([categories[i]])) } })), _ref: vref,
        barMaxWidth: 46, smooth: kind === "line" ? 0.2 : undefined, showSymbol: categories.length < 40,
        label: fmt.dataLabels ? { show: true, position: horizontal ? "right" : "top", formatter: (p) => formatValue(p.value, ctx.formatOf(vref), { compact: true }) } : undefined,
      });
    });
    lineRefs.forEach((vref) => {
      const vi = colIndex(m, vref);
      const d = new Array(categories.length).fill(null);
      for (const r of m.rows) d[catSeen.get(JSON.stringify(r[ci]))] = r[vi];
      series.push({ name: parseRef(vref).label, type: "line", yAxisIndex: fmt.secondaryAxis ? 1 : 0, data: d, _ref: vref, smooth: 0.2, symbolSize: 5 });
    });
  }
  const firstVal = valRefs[0];
  const catAxis = { type: "category", data: categories.map(catLabel), axisLabel: { hideOverlap: true }, axisTick: { alignWithLabel: true } };
  const valAxis = { type: "value", axisLabel: { formatter: axisFormatter(ctx.formatOf(firstVal)), hideOverlap: true }, splitLine: { lineStyle: { color: "#eef0f2" } } };
  option.grid = { left: horizontal ? 8 : 12, right: lineRefs.length ? 12 : 16, top: series.length > 1 ? 30 : 12, bottom: 8, containLabel: true };
  option.xAxis = horizontal ? valAxis : catAxis;
  option.yAxis = horizontal ? { ...catAxis, inverse: true } : (lineRefs.length && fmt.secondaryAxis ? [valAxis, { type: "value", axisLabel: { formatter: axisFormatter(ctx.formatOf(lineRefs[0])) }, splitLine: { show: false } }] : valAxis);
  if (series.length > 1 && fmt.showLegend !== false) option.legend = { type: "scroll", top: 0 };
  option.tooltip = {
    trigger: "axis", axisPointer: { type: kind === "bar" ? "shadow" : "line" },
    formatter: (ps) => {
      const arr = Array.isArray(ps) ? ps : [ps];
      return `<b>${escapeHtml(arr[0]?.axisValueLabel ?? "")}</b><br>` + arr.filter((p) => p.value !== null && p.value !== undefined)
        .map((p) => `${p.marker}${escapeHtml(p.seriesName)}: <b>${formatValue(p.value, ctx.formatOf(series[p.seriesIndex]._ref))}</b>`).join("<br>");
    },
  };
  if (categories.length > 30 && !horizontal) option.dataZoom = [{ type: "inside" }];
  option.series = series.map(({ _series, _ref, ...s }) => s);
  chart.setOption(option);
  chart.on("click", (e) => {
    const raw = categories[e.dataIndex];
    const parts = [[catP, raw]];
    const s = series[e.seriesIndex];
    if (s && s._series) parts.push(s._series);
    clickCategory(ctx, parts);
  });
  observe(el, chart);
}

function clickCategory(ctx, parts) {
  const key = JSON.stringify(parts.map(([, v]) => v));
  if (ctx.selectedKey === key) return ctx.onSelect(null);
  ctx.onSelect({ key, filters: parts.map(([p, v]) => basicFilter(p.table, p.column, [v])) });
}

function observe(el, chart) {
  const ro = new ResizeObserver(() => chart.resize());
  ro.observe(el);
  el._ro = ro;
}
