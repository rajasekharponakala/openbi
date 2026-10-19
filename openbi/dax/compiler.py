"""Compile the DAX subset to DuckDB SQL.

Two evaluation modes exist, as in DAX:

* **Row context** (calculated columns, RLS filters, iterator bodies,
  CALCULATE boolean filters) compiles to a scalar SQL expression over the
  columns of a base table and the tables it can reach (``RELATED``).
* **Filter context** (measures) compiles to an *outer* SQL expression over
  aggregate "leaves". Each leaf is an aggregation over a home table under a
  particular set of context modifications (``CALCULATE``/``ALL``/...). The
  query engine evaluates every distinct (home table, context) pair as one
  grouped subquery and joins the results on the visual's group keys, which
  reproduces how filter context flows through relationships.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..engine.graph import ModelGraph
from ..model.schema import SemanticModel
from ..powerquery.sql import ident, literal
from .parser import (
    BareRef,
    BinOp,
    Call,
    ColumnRef,
    InList,
    Name,
    Neg,
    Node,
    Not,
    Number,
    String,
    TableRef,
    VarBlock,
    parse,
)


class DaxError(ValueError):
    pass


Col = tuple[str, str]  # (table, column)


def col_sql(table: str, column: str) -> str:
    return f"{ident(table)}.{ident(column)}"


@dataclass(frozen=True)
class Predicate:
    """A boolean SQL condition over columns of one table."""

    table: str
    columns: frozenset[Col]
    sql: str


@dataclass(frozen=True)
class Context:
    """Accumulated filter-context modifications (from CALCULATE arguments)."""

    removed_tables: frozenset[str] = frozenset()  # filters + grouping removed
    removed_cols: frozenset[Col] = frozenset()
    ungrouped_tables: frozenset[str] = frozenset()  # grouping removed, filters kept
    ungrouped_cols: frozenset[Col] = frozenset()
    added: tuple[Predicate, ...] = ()

    def removes(self, col: Col) -> bool:
        return col[0] in self.removed_tables or col in self.removed_cols

    def ungroups(self, col: Col) -> bool:
        return self.removes(col) or col[0] in self.ungrouped_tables or col in self.ungrouped_cols

    def remove(self, tables: set[str] = frozenset(), cols: set[Col] = frozenset()) -> Context:
        rt = self.removed_tables | frozenset(tables)
        rc = self.removed_cols | frozenset(cols)
        kept = tuple(p for p in self.added if not any(c[0] in rt or c in rc for c in p.columns))
        return Context(rt, rc, self.ungrouped_tables, self.ungrouped_cols, kept)

    def ungroup(self, tables: set[str] = frozenset(), cols: set[Col] = frozenset()) -> Context:
        return Context(
            self.removed_tables,
            self.removed_cols,
            self.ungrouped_tables | frozenset(tables),
            self.ungrouped_cols | frozenset(cols),
            self.added,
        )

    def add(self, pred: Predicate) -> Context:
        return Context(
            self.removed_tables, self.removed_cols, self.ungrouped_tables, self.ungrouped_cols, self.added + (pred,)
        )


@dataclass(frozen=True)
class Leaf:
    home: str
    agg: str  # SQL aggregate template: {x} = row expression, {f} = FILTER clause
    row_sql: str
    tables: frozenset[str]  # tables the row expression / iterator filter needs joined
    context: Context
    row_filter: str | None = None  # FILTER(...) condition of an iterator table


@dataclass
class LeafRegistry:
    leaves: dict[Leaf, str] = field(default_factory=dict)

    def column_for(self, leaf: Leaf) -> str:
        if leaf not in self.leaves:
            self.leaves[leaf] = f"__l{len(self.leaves)}"
        return self.leaves[leaf]


AGG_SIMPLE = {
    "SUM": "SUM({x}){f}",
    "AVERAGE": "AVG({x}){f}",
    "MIN": "MIN({x}){f}",
    "MAX": "MAX({x}){f}",
    "COUNT": "NULLIF(COUNT({x}){f}, 0)",
    "COUNTA": "NULLIF(COUNT({x}){f}, 0)",
    "DISTINCTCOUNT": "NULLIF(COUNT(DISTINCT {x}){f}, 0)",
    "DISTINCTCOUNTNOBLANK": "NULLIF(COUNT(DISTINCT {x}){f}, 0)",
    "MEDIAN": "MEDIAN({x}){f}",
}
AGG_ITERATORS = {
    "SUMX": "SUM({x}){f}",
    "AVERAGEX": "AVG({x}){f}",
    "MINX": "MIN({x}){f}",
    "MAXX": "MAX({x}){f}",
    "COUNTX": "NULLIF(COUNT({x}){f}, 0)",
    "MEDIANX": "MEDIAN({x}){f}",
}
# DAX returns BLANK (not 0) when counting an empty set
COUNT_ROWS = "NULLIF(COUNT(*){f}, 0)"


def _blank_arith(op: str, a: str, b: str) -> str:
    # DAX: BLANK() + 1 = 1, but BLANK() + BLANK() = BLANK()
    return f"(CASE WHEN ({a}) IS NULL AND ({b}) IS NULL THEN NULL ELSE COALESCE({a}, 0) {op} COALESCE({b}, 0) END)"


class _ScalarMixin:
    """Operators and scalar functions shared by both evaluation contexts."""

    user: str | None

    def expr(self, node: Node) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def _names(self) -> dict[str, str]:  # variable name -> compiled SQL
        raise NotImplementedError

    def scalar(self, node: Node) -> str:
        if isinstance(node, Number):
            return literal(node.value)
        if isinstance(node, String):
            return literal(node.value)
        if isinstance(node, Neg):
            return f"(-{self.expr(node.operand)})"
        if isinstance(node, Not):
            return f"(NOT {self.expr(node.operand)})"
        if isinstance(node, InList):
            return f"({self.expr(node.operand)} IN ({', '.join(self.expr(i) for i in node.items)}))"
        if isinstance(node, BinOp):
            a, b = self.expr(node.left), self.expr(node.right)
            if node.op in ("+", "-"):
                return _blank_arith(node.op, a, b)
            if node.op in ("*", "/"):
                return f"({a} {node.op} {b})"
            if node.op == "^":
                return f"power({a}, {b})"
            if node.op == "&":
                return f"(COALESCE(CAST({a} AS VARCHAR), '') || COALESCE(CAST({b} AS VARCHAR), ''))"
            if node.op == "&&":
                return f"({a} AND {b})"
            if node.op == "||":
                return f"({a} OR {b})"
            return f"({a} {node.op} {b})"
        if isinstance(node, VarBlock):
            names = self._names()
            saved = dict(names)
            for name, value in node.variables:
                names[name.lower()] = self.expr(value)
            try:
                return self.expr(node.body)
            finally:
                names.clear()
                names.update(saved)
        if isinstance(node, Name):
            v = self._names().get(node.name.lower())
            if v is None:
                raise DaxError(f"unknown variable or table '{node.name}' used as a value")
            return v
        if isinstance(node, Call):
            return self.scalar_call(node)
        raise DaxError(f"unsupported expression {node!r}")

    def scalar_call(self, c: Call) -> str:
        a = c.args
        n = c.name
        x = self.expr
        if n == "TRUE":
            return "TRUE"
        if n == "FALSE":
            return "FALSE"
        if n == "BLANK":
            return "NULL"
        if n == "IF":
            if len(a) < 2:
                raise DaxError("IF requires at least two arguments")
            other = x(a[2]) if len(a) > 2 else "NULL"
            return f"(CASE WHEN {x(a[0])} THEN {x(a[1])} ELSE {other} END)"
        if n == "SWITCH":
            if len(a) < 3:
                raise DaxError("SWITCH requires at least three arguments")
            subject = a[0]
            pairs = a[1:]
            default = "NULL"
            if len(pairs) % 2 == 1:
                default = x(pairs[-1])
                pairs = pairs[:-1]
            is_true = isinstance(subject, Call) and subject.name == "TRUE"
            whens = []
            for i in range(0, len(pairs), 2):
                cond = x(pairs[i]) if is_true else f"{x(subject)} = {x(pairs[i])}"
                whens.append(f"WHEN {cond} THEN {x(pairs[i + 1])}")
            return f"(CASE {' '.join(whens)} ELSE {default} END)"
        if n == "DIVIDE":
            alt = x(a[2]) if len(a) > 2 else "NULL"
            num, den = x(a[0]), x(a[1])
            return f"(CASE WHEN ({den}) IS NULL OR ({den}) = 0 THEN {alt} ELSE ({num}) / ({den}) END)"
        if n == "ISBLANK":
            return f"(({x(a[0])}) IS NULL)"
        if n == "AND":
            return f"({x(a[0])} AND {x(a[1])})"
        if n == "OR":
            return f"({x(a[0])} OR {x(a[1])})"
        if n == "NOT":
            return f"(NOT {x(a[0])})"
        if n == "COALESCE":
            return f"COALESCE({', '.join(x(i) for i in a)})"
        if n in ("MIN", "MAX") and len(a) == 2:
            return f"{'LEAST' if n == 'MIN' else 'GREATEST'}({x(a[0])}, {x(a[1])})"
        simple = {
            "ABS": "abs",
            "SQRT": "sqrt",
            "EXP": "exp",
            "LN": "ln",
            "UPPER": "upper",
            "LOWER": "lower",
            "TRIM": "trim",
            "LEN": "length",
            "YEAR": "year",
            "MONTH": "month",
            "DAY": "day",
            "QUARTER": "quarter",
            "HOUR": "hour",
            "MINUTE": "minute",
            "WEEKNUM": "week",
        }
        if n in simple:
            return f"{simple[n]}({x(a[0])})"
        if n == "ROUND":
            return f"round({x(a[0])}, {x(a[1]) if len(a) > 1 else '0'})"
        if n in ("INT", "TRUNC"):
            return f"CAST({'floor' if n == 'INT' else 'trunc'}({x(a[0])}) AS BIGINT)"
        if n == "MOD":
            return f"({x(a[0])} % {x(a[1])})"
        if n == "POWER":
            return f"power({x(a[0])}, {x(a[1])})"
        if n == "VALUE":
            return f"TRY_CAST({x(a[0])} AS DOUBLE)"
        if n == "LEFT":
            return f"left(CAST({x(a[0])} AS VARCHAR), {x(a[1]) if len(a) > 1 else '1'})"
        if n == "RIGHT":
            return f"right(CAST({x(a[0])} AS VARCHAR), {x(a[1]) if len(a) > 1 else '1'})"
        if n == "MID":
            return f"substr(CAST({x(a[0])} AS VARCHAR), {x(a[1])}, {x(a[2])})"
        if n == "CONCATENATE":
            return f"(COALESCE(CAST({x(a[0])} AS VARCHAR), '') || COALESCE(CAST({x(a[1])} AS VARCHAR), ''))"
        if n == "CONTAINSSTRING":
            return f"contains(lower(CAST({x(a[0])} AS VARCHAR)), lower(CAST({x(a[1])} AS VARCHAR)))"
        if n == "WEEKDAY":
            # DAX default return type 1: Sunday = 1 .. Saturday = 7
            return f"(dayofweek({x(a[0])}) + 1)"
        if n == "DATE":
            return f"make_date(CAST({x(a[0])} AS BIGINT), CAST({x(a[1])} AS BIGINT), CAST({x(a[2])} AS BIGINT))"
        if n == "TODAY":
            return "current_date"
        if n == "NOW":
            return "CAST(now() AS TIMESTAMP)"
        if n == "EOMONTH":
            months = x(a[1]) if len(a) > 1 else "0"
            return f"last_day(CAST({x(a[0])} AS DATE) + to_months(CAST({months} AS INTEGER)))"
        if n == "DATEDIFF":
            unit = a[2]
            if not isinstance(unit, Name):
                raise DaxError("DATEDIFF interval must be DAY, WEEK, MONTH, QUARTER or YEAR")
            return f"date_diff({literal(unit.name.lower())}, {x(a[0])}, {x(a[1])})"
        if n == "FORMAT":
            return self._format(x(a[0]), a[1])
        if n in ("USERPRINCIPALNAME", "USERNAME", "USEROBJECTID"):
            return literal(self.user or "")
        raise DaxError(f"function {n} is not supported in this context")

    def _format(self, value: str, fmt: Node) -> str:
        if not isinstance(fmt, String):
            raise DaxError("FORMAT requires a literal format string")
        f = fmt.value
        date_map = {
            "yyyy": "%Y",
            "yy": "%y",
            "mmmm": "%B",
            "mmm": "%b",
            "mm": "%m",
            "dd": "%d",
            "dddd": "%A",
            "ddd": "%a",
            "q": "%q",
        }
        if any(k in f.lower() for k in ("yy", "mm", "dd")):
            out, i, low = "", 0, f
            keys = sorted(date_map, key=len, reverse=True)
            while i < len(low):
                for k in keys:
                    if low[i : i + len(k)].lower() == k:
                        out += date_map[k]
                        i += len(k)
                        break
                else:
                    out += low[i]
                    i += 1
            return f"strftime(CAST({value} AS TIMESTAMP), {literal(out)})"
        if f in ("0", "#,0", "#,##0"):
            return f"format('{{:,.0f}}', CAST({value} AS DOUBLE))"
        if f in ("0.00", "#,0.00", "#,##0.00"):
            return f"format('{{:,.2f}}', CAST({value} AS DOUBLE))"
        if f in ("0%", "0.0%", "0.00%"):
            digits = len(f) - 2 - (1 if "." in f else 0)
            return f"format('{{:.{max(digits, 0)}f}}%', 100 * CAST({value} AS DOUBLE))"
        return f"CAST({value} AS VARCHAR)"


class RowCompiler(_ScalarMixin):
    """Row-context compiler over ``base`` and the tables reachable from it."""

    def __init__(
        self,
        model: SemanticModel,
        graph: ModelGraph,
        base: str,
        user: str | None = None,
        allow_related: bool = True,
        variables: dict[str, str] | None = None,
    ):
        self.model = model
        self.graph = graph
        self.base = graph.canonical(base)
        self.user = user
        self.allow_related = allow_related
        self.reach = graph.reachable(self.base) if allow_related else {self.base: []}
        self.tables: set[str] = {self.base}
        self.columns: set[Col] = set()
        self._vars: dict[str, str] = dict(variables or {})

    def _names(self) -> dict[str, str]:
        return self._vars

    def _col(self, table: str, column: str) -> str:
        t = self.graph.canonical(table)
        if t not in self.reach:
            raise DaxError(f"column {t}[{column}] is not reachable from '{self.base}' in row context")
        self.tables.add(t)
        self.columns.add((t, column))
        return col_sql(t, column)

    def expr(self, node: Node) -> str:
        if isinstance(node, ColumnRef):
            return self._col(node.table, node.column)
        if isinstance(node, BareRef):
            if self.model.measure(node.name) is not None:
                raise DaxError(
                    f"measure [{node.name}] cannot be used in a row context (context transition is not supported)"
                )
            return self._col(self.base, node.name)
        if isinstance(node, Call) and node.name == "RELATED":
            if not node.args or not isinstance(node.args[0], ColumnRef):
                raise DaxError("RELATED requires a column reference")
            return self.expr(node.args[0])
        return self.scalar(node)


def compile_row(
    model: SemanticModel, graph: ModelGraph, table: str, expression: str, user: str | None = None
) -> tuple[str, set[str]]:
    """Compile a row-context DAX expression. Returns (sql, tables needed)."""
    rc = RowCompiler(model, graph, table, user=user)
    sql = rc.expr(parse(expression))
    return sql, rc.tables


@dataclass
class CompiledMeasure:
    name: str
    sql: str  # over leaf columns produced by the registry


class MeasureCompiler(_ScalarMixin):
    """Filter-context compiler for measures."""

    def __init__(self, model: SemanticModel, graph: ModelGraph, registry: LeafRegistry, user: str | None = None):
        self.model = model
        self.graph = graph
        self.registry = registry
        self.user = user
        self._ctx = Context()
        self._vars: dict[str, str] = {}
        self._stack: list[str] = []

    def _names(self) -> dict[str, str]:
        return self._vars

    # -- public -------------------------------------------------------
    def compile_measure(self, name: str) -> CompiledMeasure:
        found = self.model.measure(name)
        if found is None:
            raise DaxError(f"unknown measure [{name}]")
        _, m = found
        return CompiledMeasure(m.name, self._measure_ref(m.name))

    def compile_expression(self, expression: str) -> str:
        return self.expr(parse(expression))

    # -- helpers ------------------------------------------------------
    def _measure_ref(self, name: str) -> str:
        found = self.model.measure(name)
        if found is None:
            raise DaxError(f"unknown measure [{name}]")
        _, m = found
        key = m.name.lower()
        if key in self._stack:
            raise DaxError(f"circular measure reference: {' -> '.join(self._stack + [key])}")
        self._stack.append(key)
        saved_vars = self._vars
        self._vars = {}
        try:
            return f"({self.expr(parse(m.expression))})"
        finally:
            self._vars = saved_vars
            self._stack.pop()

    def _table_name(self, node: Node) -> str | None:
        if isinstance(node, TableRef):
            return self.graph.canonical(node.name)
        if isinstance(node, Name) and node.name.lower() not in self._vars:
            return self.graph.canonical(node.name)
        return None

    def _leaf(self, home: str, agg: str, row_sql: str, tables: set[str], row_filter: str | None, ctx: Context) -> str:
        leaf = Leaf(self.graph.canonical(home), agg, row_sql, frozenset(tables), ctx, row_filter)
        return ident(self.registry.column_for(leaf))

    def _table_expr(self, node: Node, ctx: Context) -> tuple[str, list[str], set[str], Context]:
        """Resolve an iterator's table argument: (home, filter sqls, tables, ctx)."""
        t = self._table_name(node)
        if t is not None:
            return t, [], {t}, ctx
        if isinstance(node, Call) and node.name in ("ALL", "REMOVEFILTERS", "ALLNOBLANKROW") and len(node.args) == 1:
            t = self._table_name(node.args[0])
            if t is None:
                raise DaxError(f"{node.name}() as an iterator table must name a table")
            return t, [], {t}, ctx.remove(tables=self.graph.expanded(t))
        if isinstance(node, Call) and node.name == "FILTER" and len(node.args) == 2:
            home, filters, tables, ctx2 = self._table_expr(node.args[0], ctx)
            rc = RowCompiler(self.model, self.graph, home, user=self.user, variables=self._vars)
            cond = rc.expr(node.args[1])
            return home, filters + [cond], tables | rc.tables, ctx2
        raise DaxError(f"unsupported table expression {node!r}")

    def _apply_modifier(self, ctx: Context, node: Node) -> Context:
        if isinstance(node, Call):
            n = node.name
            if n in ("ALL", "REMOVEFILTERS", "ALLNOBLANKROW"):
                if not node.args:
                    return ctx.remove(tables={t.name for t in self.model.tables})
                tables, cols = set(), set()
                for arg in node.args:
                    if isinstance(arg, ColumnRef):
                        cols.add((self.graph.canonical(arg.table), arg.column))
                    else:
                        t = self._table_name(arg)
                        if t is None:
                            raise DaxError(f"{n} expects tables or columns")
                        tables |= self.graph.expanded(t)
                return ctx.remove(tables=tables, cols=cols)
            if n == "ALLEXCEPT":
                t = self._table_name(node.args[0])
                if t is None:
                    raise DaxError("ALLEXCEPT expects a table as first argument")
                keep = {(self.graph.canonical(a.table), a.column) for a in node.args[1:] if isinstance(a, ColumnRef)}
                keep_tables = {k[0] for k in keep}
                removed_tables = self.graph.expanded(t) - keep_tables
                cols: set[Col] = set()
                for kt in keep_tables:
                    tbl = self.model.table(kt)
                    for c in tbl.columns if tbl else []:
                        if (kt, c.name) not in keep:
                            cols.add((kt, c.name))
                return ctx.remove(tables=removed_tables, cols=cols)
            if n == "ALLSELECTED":
                if not node.args:
                    return ctx.ungroup(tables={t.name for t in self.model.tables})
                tables, cols = set(), set()
                for arg in node.args:
                    if isinstance(arg, ColumnRef):
                        cols.add((self.graph.canonical(arg.table), arg.column))
                    else:
                        t = self._table_name(arg)
                        if t:
                            tables |= self.graph.expanded(t)
                return ctx.ungroup(tables=tables, cols=cols)
            if n == "KEEPFILTERS":
                pred = self._predicate(node.args[0])
                return ctx.add(pred)
            if n == "FILTER":
                home, filters, _tables, ctx2 = self._table_expr(node, ctx)
                # FILTER(table, cond) keeps existing filters and adds cond.
                rc = RowCompiler(self.model, self.graph, home, user=self.user, variables=self._vars)
                rc.expr(node.args[1])
                cols = rc.columns
                for f in filters:
                    ctx2 = ctx2.add(Predicate(home, frozenset(cols), f))
                return ctx2
            if n == "USERELATIONSHIP" or n == "CROSSFILTER":
                raise DaxError(f"{n} is not supported yet")
        # Boolean filter: replaces existing filters on the referenced columns.
        pred = self._predicate(node)
        return ctx.remove(cols=set(pred.columns)).add(pred)

    def _predicate(self, node: Node) -> Predicate:
        table = _first_table(node)
        if table is None:
            raise DaxError("a CALCULATE filter must reference a column")
        rc = RowCompiler(self.model, self.graph, table, user=self.user, allow_related=False, variables=self._vars)
        sql = rc.expr(node)
        return Predicate(rc.base, frozenset(rc.columns), sql)

    # -- dispatch -----------------------------------------------------
    def expr(self, node: Node) -> str:
        if isinstance(node, BareRef):
            if self.model.measure(node.name) is None:
                raise DaxError(f"[{node.name}] is not a measure; use Table[{node.name}] inside an aggregation")
            return self._measure_ref(node.name)
        if isinstance(node, ColumnRef):
            raise DaxError(
                f"a column reference {node.table}[{node.column}] cannot be used in a measure without an aggregation"
            )
        if isinstance(node, Call):
            return self._call(node)
        return self.scalar(node)

    def _call(self, c: Call) -> str:
        n, a, ctx = c.name, c.args, self._ctx
        if n in AGG_SIMPLE and len(a) == 1:
            if not isinstance(a[0], ColumnRef):
                raise DaxError(f"{n} requires a column reference")
            home = self.graph.canonical(a[0].table)
            row = col_sql(home, a[0].column)
            return self._leaf(home, AGG_SIMPLE[n], row, {home}, None, ctx)
        if n == "COUNTROWS":
            if not a:
                raise DaxError("COUNTROWS requires a table")
            home, filters, tables, ctx2 = self._table_expr(a[0], ctx)
            return self._leaf(home, COUNT_ROWS, "1", tables, _and(filters), ctx2)
        if n == "COUNTBLANK":
            if not isinstance(a[0], ColumnRef):
                raise DaxError("COUNTBLANK requires a column reference")
            home = self.graph.canonical(a[0].table)
            return self._leaf(home, COUNT_ROWS, "1", {home}, f"{col_sql(home, a[0].column)} IS NULL", ctx)
        if n in AGG_ITERATORS:
            if len(a) != 2:
                raise DaxError(f"{n} requires a table and an expression")
            home, filters, tables, ctx2 = self._table_expr(a[0], ctx)
            rc = RowCompiler(self.model, self.graph, home, user=self.user, variables=self._vars)
            row = rc.expr(a[1])
            return self._leaf(home, AGG_ITERATORS[n], row, tables | rc.tables, _and(filters), ctx2)
        if n in ("CALCULATE", "CALCULATETABLE"):
            if not a:
                raise DaxError("CALCULATE requires an expression")
            new_ctx = ctx
            for mod in a[1:]:
                new_ctx = self._apply_modifier(new_ctx, mod)
            self._ctx = new_ctx
            try:
                return self.expr(a[0])
            finally:
                self._ctx = ctx
        if n in ("SELECTEDVALUE", "HASONEVALUE", "ISFILTERED", "VALUES"):
            if not a or not isinstance(a[0], ColumnRef):
                raise DaxError(f"{n} requires a column reference")
            home = self.graph.canonical(a[0].table)
            row = col_sql(home, a[0].column)
            distinct = self._leaf(home, "COUNT(DISTINCT {x}){f}", row, {home}, None, ctx)
            if n == "HASONEVALUE" or n == "ISFILTERED":
                return f"({distinct} = 1)"
            value = self._leaf(home, "MIN({x}){f}", row, {home}, None, ctx)
            alt = self.expr(a[1]) if len(a) > 1 else "NULL"
            return f"(CASE WHEN {distinct} = 1 THEN {value} ELSE {alt} END)"
        return self.scalar_call(c)


def _and(conds: list[str]) -> str | None:
    if not conds:
        return None
    return " AND ".join(f"({c})" for c in conds)


def _first_table(node: Node) -> str | None:
    if isinstance(node, ColumnRef):
        return node.table
    for v in vars(node).values() if hasattr(node, "__dict__") else []:
        items = v if isinstance(v, tuple) else (v,)
        for item in items:
            if isinstance(item, tuple):
                item = item[1] if len(item) == 2 else None
            if isinstance(item, Node):
                t = _first_table(item)
                if t:
                    return t
    return None


MeasureResolver = Callable[[str], CompiledMeasure]
