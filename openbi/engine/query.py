"""Semantic query engine.

Plays the role of the Analysis Services formula engine: a visual asks for
group-by columns, measures and filters (like DAX ``SUMMARIZECOLUMNS``) and
this module generates one DuckDB SQL statement that evaluates it with
filter-context semantics:

* filters on a dimension reach a fact through many-to-one relationships
  (and back through relationships marked ``both``);
* each measure leaf is aggregated in its own home table, so measures from
  different fact tables can share one visual;
* ``CALCULATE`` modifiers become separate subqueries grouped by the keys
  that remain in context, joined back on those keys;
* row-level-security filters are applied to every subquery and can never
  be removed by ``ALL``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import duckdb

from ..dax.compiler import (
    Col,
    Context,
    DaxError,
    Leaf,
    LeafRegistry,
    MeasureCompiler,
    Predicate,
    RowCompiler,
    col_sql,
)
from ..dax.parser import BareRef, ColumnRef, parse
from ..model.schema import SemanticModel
from ..powerquery.sql import ident, literal
from .filters import TopNFilter, parse_filters
from .graph import Edge, ModelGraph, UnknownTable


class QueryError(ValueError):
    pass


@dataclass
class SelectItem:
    name: str  # output column name
    column: Col | None = None
    measure: str | None = None  # model measure name
    expression: str | None = None  # ad-hoc DAX measure expression


@dataclass
class SemanticQuery:
    select: list[SelectItem]
    filters: list[dict[str, Any]] = field(default_factory=list)
    order_by: list[tuple[str, bool]] = field(default_factory=list)  # (output name, descending)
    top: int | None = None


@dataclass
class QueryResult:
    columns: list[dict[str, str]]
    rows: list[list[Any]]
    sql: str

    def as_dict(self) -> dict[str, Any]:
        return {"columns": self.columns, "rows": self.rows}


AGG_FOR_FIELD = {
    "sum": "SUM",
    "average": "AVERAGE",
    "avg": "AVERAGE",
    "min": "MIN",
    "max": "MAX",
    "count": "COUNT",
    "distinctCount": "DISTINCTCOUNT",
    "median": "MEDIAN",
}


def parse_select(items: list[Any]) -> list[SelectItem]:
    """Parse select entries from the JSON API.

    Accepted forms: ``"Table[Column]"``, ``"[Measure]"``,
    ``{"column": "Table[Column]", "aggregate": "sum"}`` (implicit measure),
    ``{"expression": "SUM(T[c])", "name": "Alias"}``.
    """
    out: list[SelectItem] = []
    for it in items:
        if isinstance(it, str):
            it = {"field": it}
        if not isinstance(it, dict):
            raise QueryError(f"invalid select item {it!r}")
        if "expression" in it:
            out.append(SelectItem(name=it.get("name") or it["expression"], expression=it["expression"]))
            continue
        ref = it.get("field") or it.get("column") or it.get("measure")
        if not ref:
            raise QueryError(f"invalid select item {it!r}")
        if "measure" in it and "[" not in ref:
            ref = f"[{ref}]"
        node = parse(ref)
        if isinstance(node, ColumnRef):
            agg = it.get("aggregate")
            if agg:
                fn = AGG_FOR_FIELD.get(agg)
                if fn is None:
                    raise QueryError(f"unknown aggregate '{agg}'")
                name = it.get("name") or f"{agg.capitalize()} of {node.column}"
                out.append(SelectItem(name=name, expression=f"{fn}({ref})"))
            else:
                out.append(
                    SelectItem(name=it.get("name") or f"{node.table}[{node.column}]", column=(node.table, node.column))
                )
        elif isinstance(node, BareRef):
            out.append(SelectItem(name=it.get("name") or node.name, measure=node.name))
        else:
            raise QueryError(f"select item must be a column or measure reference: {ref!r}")
    return out


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return None
    return v


class QueryEngine:
    def __init__(
        self,
        model: SemanticModel,
        roles: list[str] | None = None,
        user: str | None = None,
        today: date | None = None,
    ):
        self.model = model
        self.graph = ModelGraph(model)
        self.user = user
        self.today = today
        self.roles = roles or []
        self._rls = self._rls_predicates()

    # -- security -----------------------------------------------------
    def _rls_predicates(self) -> list[Predicate]:
        if not self.roles:
            return []
        per_table: dict[str, list[str | None]] = {}
        roles = []
        for name in self.roles:
            role = self.model.role(name)
            if role is None:
                raise QueryError(f"unknown role '{name}'")
            roles.append(role)
        tables = {self.graph.canonical(tp.table) for r in roles for tp in r.table_permissions}
        for t in tables:
            per_table[t] = []
            for r in roles:
                tp = next((p for p in r.table_permissions if self.graph.canonical(p.table) == t), None)
                if tp is None:
                    per_table[t].append(None)  # role doesn't restrict this table
                else:
                    rc = RowCompiler(self.model, self.graph, t, user=self.user, allow_related=False)
                    per_table[t].append(rc.expr(parse(tp.filter_expression)))
        preds = []
        for t, conds in per_table.items():
            if any(c is None for c in conds):
                continue  # union of roles: an unrestricted role grants all rows
            sql = " OR ".join(f"COALESCE({c}, FALSE)" for c in conds)  # type: ignore[union-attr]
            preds.append(Predicate(t, frozenset(), f"({sql})"))
        return preds

    # -- SQL generation ------------------------------------------------
    def _joins(self, home: str, tables: set[str]) -> str:
        reach = self.graph.reachable(home)
        seen: set[str] = {home}
        parts: list[str] = []
        for t in sorted(tables, key=lambda x: len(reach.get(x, []))):
            for e in reach[t]:
                if e.dst in seen:
                    continue
                seen.add(e.dst)
                parts.append(f" LEFT JOIN {ident(e.dst)} ON {col_sql(e.src, e.src_col)} = {col_sql(e.dst, e.dst_col)}")
        return "".join(parts)

    def _where(self, home: str, preds: list[Predicate], needed: set[str]) -> list[str]:
        """Conditions applicable to a scan of ``home``; adds join needs to ``needed``."""
        reach = self.graph.reachable(home)
        out: list[str] = []
        pending: list[Predicate] = []
        for p in preds:
            tables = {c[0] for c in p.columns} | {p.table}
            if tables <= set(reach):
                needed |= tables
                out.append(p.sql)
            else:
                pending.append(p)
        if not pending:
            return out
        # Bidirectional cross-filtering: filters on a many-side table flow
        # back to the one-side table through a semi-join.
        for t in sorted(reach):
            for e in self.graph.bidirectional_into(t):
                if e.src in reach:
                    continue
                many_reach = set(self.graph.reachable(e.src))
                applicable = [p for p in pending if ({c[0] for c in p.columns} | {p.table}) <= many_reach]
                if not applicable:
                    continue
                inner_needed: set[str] = set()
                for p in applicable:
                    inner_needed |= {c[0] for c in p.columns} | {p.table}
                conds = " AND ".join(p.sql for p in applicable)
                sub = f"SELECT {col_sql(e.src, e.src_col)} FROM {ident(e.src)}{self._joins(e.src, inner_needed)} WHERE {conds}"
                needed.add(t)
                out.append(f"{col_sql(t, e.dst_col)} IN ({sub})")
                pending = [p for p in pending if p not in applicable]
        return out

    def _bridges(
        self, home: str, keys: list[tuple[str, Col]]
    ) -> tuple[list[tuple[str, Col]], list[tuple[Edge, list[tuple[str, Col]]]]]:
        """Split group keys into directly reachable ones and ones reachable
        only through a bidirectional relationship (grouped via a bridge)."""
        reach = self.graph.reachable(home)
        direct = [(a, c) for a, c in keys if c[0] in reach]
        rest = [(a, c) for a, c in keys if c[0] not in reach]
        bridges: list[tuple[Edge, list[tuple[str, Col]]]] = []
        for t in sorted(reach):
            for e in self.graph.bidirectional_into(t):
                if not rest or e.src in reach:
                    continue
                many = self.graph.reachable(e.src)
                via = [(a, c) for a, c in rest if c[0] in many]
                if via:
                    bridges.append((e, via))
                    rest = [k for k in rest if k not in via]
        return direct, bridges

    def _scan(
        self,
        home: str,
        keys: list[tuple[str, Col]],
        aggs: list[str],
        preds: list[Predicate],
        extra_tables: set[str],
    ) -> str:
        direct, bridges = self._bridges(home, keys)
        needed: set[str] = set(extra_tables) | {k[1][0] for k in direct} | {e.dst for e, _ in bridges}
        conds = self._where(home, preds + self._rls, needed)
        select = [f"{col_sql(*c)} AS {ident(alias)}" for alias, c in direct]
        bridge_sql = ""
        for i, (e, via) in enumerate(bridges):
            b = ident(f"__br{i}")
            inner_cols = ", ".join(f"{col_sql(*c)} AS {ident(a)}" for a, c in via)
            inner = (
                f"SELECT DISTINCT {col_sql(e.src, e.src_col)} AS __b, {inner_cols} "
                f"FROM {ident(e.src)}{self._joins(e.src, {c[0] for _, c in via})}"
            )
            bridge_sql += f" JOIN ({inner}) AS {b} ON {col_sql(e.dst, e.dst_col)} = {b}.__b"
            select += [f"{b}.{ident(a)} AS {ident(a)}" for a, _ in via]
        keys = direct + [k for _, via in bridges for k in via]
        select += aggs
        sql = f"SELECT {', '.join(select) or '1 AS __one'} FROM {ident(home)}{self._joins(home, needed)}{bridge_sql}"
        if conds:
            sql += " WHERE " + " AND ".join(f"({c})" for c in conds)
        if keys and aggs:
            sql += " GROUP BY " + ", ".join(ident(a) for a, _ in keys)
        elif keys:
            sql = f"SELECT DISTINCT * FROM ({sql})"
        return sql

    def _scan_keys(self, home: str, keys: list[tuple[str, Col]]) -> list[tuple[str, Col]]:
        direct, bridges = self._bridges(home, keys)
        return direct + [k for _, via in bridges for k in via]

    def _reaches(self, home: str, cols: list[Col]) -> bool:
        keys = [(str(i), c) for i, c in enumerate(cols)]
        return len(self._scan_keys(home, keys)) == len(keys)

    def build_sql(self, q: SemanticQuery, extra_predicates: list[Predicate] | None = None) -> tuple[str, list[str]]:
        try:
            return self._build_sql(q, extra_predicates)
        except (UnknownTable, DaxError) as e:
            raise QueryError(str(e)) from e

    def _build_sql(self, q: SemanticQuery, extra_predicates: list[Predicate] | None) -> tuple[str, list[str]]:
        canonical = self.graph.canonical
        names = [i.name for i in q.select]
        if len(names) != len(set(names)):
            raise QueryError("select items must have unique names")
        parsed = parse_filters(q.filters, canonical, self.today)
        preds = parsed.predicates + (extra_predicates or [])
        preds += [self._topn_predicate(tn, preds) for tn in parsed.topn]

        # Group keys (plus hidden sort-by columns)
        keys: list[tuple[str, Col]] = []
        out_cols: list[tuple[str, str]] = []  # (output name, internal alias)
        sort_keys: dict[str, str] = {}
        for item in q.select:
            if item.column is None:
                continue
            t = canonical(item.column[0])
            table = self.model.table(t)
            col = (t, item.column[1])
            alias = f"__g{len(keys)}"
            keys.append((alias, col))
            out_cols.append((item.name, alias))
            meta = table.column(col[1]) if table else None
            if meta and meta.sort_by_column:
                s_alias = f"__g{len(keys)}"
                keys.append((s_alias, (t, meta.sort_by_column)))
                sort_keys[item.name] = s_alias

        # Measures
        registry = LeafRegistry()
        mc = MeasureCompiler(self.model, self.graph, registry, user=self.user)
        measures: list[tuple[str, str]] = []  # (output name, sql)
        try:
            for item in q.select:
                if item.measure:
                    measures.append((item.name, mc.compile_measure(item.measure).sql))
                elif item.expression:
                    measures.append((item.name, mc.compile_expression(item.expression)))
            hidden: list[tuple[str, str]] = []
            names = {n for n, _ in measures}
            for mf in parsed.measure_filters:
                if mf.measure not in names:
                    hidden.append((mf.measure, mc.compile_measure(mf.measure).sql))
                    names.add(mf.measure)
        except DaxError as e:
            raise QueryError(str(e)) from e

        all_measures = measures + hidden
        groups: dict[tuple[str, Context], list[tuple[Leaf, str]]] = {}
        for leaf, colname in registry.leaves.items():
            groups.setdefault((leaf.home, leaf.context), []).append((leaf, colname))

        ctes: list[str] = []
        joins: list[str] = []
        full_cols = [c for _, c in keys]
        spine_parts: list[str] = []
        for i, ((home, ctx), leaves) in enumerate(groups.items()):
            sub_keys = self._scan_keys(home, [(a, c) for a, c in keys if not ctx.ungroups(c)])
            sub_preds = [p for p in preds if not any(ctx.removes(c) for c in p.columns)] + list(ctx.added)
            aggs = []
            extra: set[str] = set()
            for leaf, colname in leaves:
                filt = f" FILTER (WHERE {leaf.row_filter})" if leaf.row_filter else ""
                aggs.append(f"{leaf.agg.replace('{f}', filt).replace('{x}', leaf.row_sql)} AS {ident(colname)}")
                extra |= set(leaf.tables)
            name = f"__q{i}"
            ctes.append(f"{name} AS ({self._scan(home, sub_keys, aggs, sub_preds, extra)})")
            on = " AND ".join(f"__spine.{ident(a)} IS NOT DISTINCT FROM {name}.{ident(a)}" for a, _ in sub_keys)
            joins.append(f" LEFT JOIN {name} ON {on}" if on else f" CROSS JOIN {name}")
            if len(sub_keys) == len(keys) and keys:
                spine_parts.append(f"SELECT {', '.join(ident(a) for a, _ in keys)} FROM {name}")

        if keys:
            if not spine_parts:
                homes = list(dict.fromkeys(h for h, _ in groups)) or self._candidate_homes(full_cols)
                for h in homes:
                    if self._reaches(h, full_cols):
                        spine_parts.append(self._scan(h, keys, [], preds, set()))
                if not spine_parts:
                    # unrelated tables: cross join each table's distinct values
                    by_table: dict[str, list[tuple[str, Col]]] = {}
                    for a, c in keys:
                        by_table.setdefault(c[0], []).append((a, c))
                    parts = [f"({self._scan(t, ks, [], preds, set())})" for t, ks in by_table.items()]
                    spine_parts.append("SELECT * FROM " + " CROSS JOIN ".join(parts))
            spine = " UNION ".join(spine_parts)
        else:
            spine = "SELECT 1 AS __one"

        select = [f"__spine.{ident(alias)} AS {ident(name)}" for name, alias in out_cols]
        select += [f"__spine.{ident(alias)} AS {ident('__sort_' + name)}" for name, alias in sort_keys.items()]
        select += [f"{sql} AS {ident(name)}" for name, sql in all_measures]
        with_clause = "WITH " + ", ".join(ctes + [f"__spine AS ({spine})"])
        inner = f"{with_clause} SELECT {', '.join(select)} FROM __spine{''.join(joins)}"

        where = []
        if all_measures and keys:
            where.append("(" + " OR ".join(f"{ident(n)} IS NOT NULL" for n, _ in all_measures) + ")")
        for mf in parsed.measure_filters:
            where.append(mf.sql.replace("{m}", ident(mf.measure)))
        visible = [item.name for item in q.select]  # keep the caller's column order
        sql = f"SELECT {', '.join(ident(n) for n in visible)} FROM ({inner}) AS __r"
        if where:
            sql += " WHERE " + " AND ".join(where)
        order = []
        for name, desc in q.order_by:
            if name not in visible:
                raise QueryError(f"cannot order by '{name}': not in select")
            target = f"__sort_{name}" if name in sort_keys else name
            order.append(f"{ident(target)} {'DESC' if desc else 'ASC'} NULLS LAST")
        if not order:
            order = [f"{ident('__sort_' + n if n in sort_keys else n)} ASC NULLS LAST" for n, _ in out_cols]
        if order:
            sql += " ORDER BY " + ", ".join(order)
        if q.top is not None:
            sql += f" LIMIT {int(q.top)}"
        return sql, visible

    def _candidate_homes(self, cols: list[Col]) -> list[str]:
        tables = {c[0] for c in cols}
        if len(tables) == 1:
            return [next(iter(tables))]
        return [t.name for t in self.model.tables if self._reaches(t.name, cols)]

    def _topn_predicate(self, tn: TopNFilter, preds: list[Predicate]) -> Predicate:
        if self._con is None:
            raise QueryError("TopN filters need a connection")
        if tn.order_measure:
            m = SelectItem(name="__v", measure=tn.order_measure)
        elif tn.order_expression:
            m = SelectItem(name="__v", expression=tn.order_expression)
        else:
            raise QueryError("TopN filter requires orderBy.measure")
        sub = SemanticQuery(
            select=[SelectItem(name="__k", column=(tn.table, tn.column)), m],
            filters=[],
            order_by=[("__v", tn.operator != "Bottom")],
            top=tn.count,
        )
        # the other filters still apply when ranking
        sql, _ = self.build_sql(sub, preds)
        values = [r[0] for r in self._con.execute(sql).fetchall()]
        col = col_sql(tn.table, tn.column)
        cond = f"{col} IN ({', '.join(literal(v) for v in values if v is not None)})" if values else "FALSE"
        return Predicate(tn.table, frozenset({(tn.table, tn.column)}), cond)

    # -- execution -----------------------------------------------------
    _con: duckdb.DuckDBPyConnection | None = None

    def execute(self, con: duckdb.DuckDBPyConnection, q: SemanticQuery, timeout: float | None = 30.0) -> QueryResult:
        self._con = con
        sql, visible = self.build_sql(q)
        timer = None
        if timeout:
            timer = threading.Timer(timeout, con.interrupt)
            timer.start()
        try:
            cur = con.execute(sql)
            rows = cur.fetchall()
            desc = cur.description or []
        except duckdb.InterruptException as e:
            raise QueryError(f"query exceeded {timeout}s timeout") from e
        except duckdb.Error as e:
            raise QueryError(f"query failed: {e}") from e
        finally:
            if timer:
                timer.cancel()
        columns = [{"name": d[0], "type": str(d[1])} for d in desc]
        return QueryResult(columns, [[_jsonable(v) for v in r] for r in rows], sql)
