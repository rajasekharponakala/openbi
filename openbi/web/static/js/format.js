// Formatting of values with model format strings (a practical subset of
// the Excel/.NET format strings that Power BI models use), via Intl.

const cache = new Map();

function parseFormat(fmt) {
  if (cache.has(fmt)) return cache.get(fmt);
  const percent = fmt.includes("%");
  const prefix = (fmt.match(/^[^#0.,%]*/) || [""])[0].replace(/"/g, "");
  const suffix = (fmt.match(/[^#0.,%]*$/) || [""])[0].replace(/"/g, "");
  const dec = fmt.includes(".") ? (fmt.split(".")[1].match(/[0#]/g) || []).length : 0;
  const group = fmt.includes(",");
  const spec = { percent, prefix, suffix, dec, group };
  cache.set(fmt, spec);
  return spec;
}

export function isNumber(v) { return typeof v === "number" && Number.isFinite(v); }

export function formatValue(v, fmt, { compact = false } = {}) {
  if (v === null || v === undefined) return "";
  if (typeof v === "boolean") return v ? "True" : "False";
  if (!isNumber(v)) return String(v);
  if (fmt === "General") return String(v); // plain column values (years, codes, keys)
  if (!fmt) {
    if (compact) return compactNumber(v);
    return Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  const f = parseFormat(fmt);
  if (f.percent) {
    return f.prefix + (v * 100).toLocaleString(undefined, { minimumFractionDigits: f.dec, maximumFractionDigits: f.dec }) + "%" + f.suffix.replace("%", "");
  }
  if (compact && Math.abs(v) >= 1000) return f.prefix + compactNumber(v) + f.suffix;
  return f.prefix + v.toLocaleString(undefined, { minimumFractionDigits: f.dec, maximumFractionDigits: f.dec, useGrouping: f.group }) + f.suffix;
}

export function compactNumber(v) {
  const a = Math.abs(v);
  const units = [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]];
  for (const [n, s] of units) {
    if (a >= n) {
      const x = v / n;
      return (Math.abs(x) >= 100 ? x.toFixed(0) : Math.abs(x) >= 10 ? x.toFixed(1) : x.toFixed(2)) + s;
    }
  }
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
}

export function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
