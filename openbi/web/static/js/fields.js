// Field references used in report definitions:
//   "Table[Column]"                      a column (grouping)
//   "[Measure]"                          a model measure
//   {field: "Table[Column]", aggregate}  an implicit measure (e.g. Sum of Quantity)

const COL_RE = /^(?:'((?:[^']|'')+)'|([^\[\]']+))\[((?:[^\]]|\]\])+)\]$/;
const MEASURE_RE = /^\[((?:[^\]]|\]\])+)\]$/;

export const AGGREGATES = [
  ["sum", "Sum"], ["average", "Average"], ["min", "Minimum"], ["max", "Maximum"],
  ["count", "Count"], ["distinctCount", "Count (Distinct)"], ["median", "Median"],
];

export function columnRef(table, column) {
  const t = /^[A-Za-z_][A-Za-z0-9_]*$/.test(table) ? table : `'${table.replace(/'/g, "''")}'`;
  return `${t}[${column.replace(/\]/g, "]]")}]`;
}

export function measureRef(name) { return `[${name.replace(/\]/g, "]]")}]`; }

export function parseRef(ref) {
  if (ref && typeof ref === "object") {
    const inner = parseRef(ref.field);
    const agg = AGGREGATES.find((a) => a[0] === ref.aggregate);
    return {
      kind: "implicit", table: inner.table, column: inner.column, aggregate: ref.aggregate, ref,
      name: ref.name || `${agg ? agg[1] : ref.aggregate} of ${inner.column}`,
      label: ref.name || `${agg ? agg[1] : ref.aggregate} of ${inner.column}`,
    };
  }
  let m = COL_RE.exec(ref);
  if (m) {
    const table = (m[1] ? m[1].replace(/''/g, "'") : m[2]).trim();
    const column = m[3].replace(/]]/g, "]");
    return { kind: "column", table, column, ref, name: ref, label: column };
  }
  m = MEASURE_RE.exec(ref);
  if (m) {
    const name = m[1].replace(/]]/g, "]");
    return { kind: "measure", name, ref, label: name };
  }
  throw new Error(`invalid field reference: ${ref}`);
}

export function isValueRef(ref) {
  const p = parseRef(ref);
  return p.kind !== "column";
}

/** Entry for the query API `select`. */
export function selectItem(ref) {
  const p = parseRef(ref);
  if (p.kind === "implicit") return { column: columnRef(p.table, p.column), aggregate: p.aggregate, name: p.name };
  return { field: p.ref, name: p.name };
}

export function outputName(ref) { return parseRef(ref).name; }

export function basicFilter(table, column, values, operator = "In") {
  return {
    $schema: "http://powerbi.com/product/schema#basic",
    filterType: 1,
    target: { table, column },
    operator,
    values,
  };
}

export function advancedFilter(table, column, logicalOperator, conditions) {
  return {
    $schema: "http://powerbi.com/product/schema#advanced",
    filterType: 0,
    target: { table, column },
    logicalOperator,
    conditions,
  };
}

export function topNFilter(table, column, operator, itemCount, measure) {
  return {
    $schema: "http://powerbi.com/product/schema#topN",
    filterType: 5,
    target: { table, column },
    operator,
    itemCount,
    orderBy: { measure },
  };
}

export function filterTargetLabel(f) {
  const t = f.target || {};
  return t.column ? `${t.table}[${t.column}]` : t.measure ? `[${t.measure}]` : "?";
}

export function describeFilter(f) {
  const ft = f.filterType ?? (f.conditions ? 0 : f.itemCount ? 5 : 1);
  if (ft === 1) {
    if (f.operator === "All" || !(f.values || []).length) return "is (All)";
    const vs = f.values.map((v) => (v === null ? "(Blank)" : String(v)));
    return `${f.operator === "NotIn" ? "is not" : "is"} ${vs.slice(0, 3).join(", ")}${vs.length > 3 ? ` +${vs.length - 3}` : ""}`;
  }
  if (ft === 0) return (f.conditions || []).map((c) => `${c.operator} ${c.value ?? ""}`).join(` ${f.logicalOperator || "And"} `);
  if (ft === 5) return `${f.operator} ${f.itemCount} by ${f.orderBy?.measure ?? "?"}`;
  if (ft === 4) return `in ${["last", "this", "next"][f.operator] ?? "?"} ${f.timeUnitsCount} units`;
  return "custom";
}
