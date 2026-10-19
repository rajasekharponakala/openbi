"""Power Query equivalent: applied steps compiled to DuckDB SQL.

Power BI's Power Query records a list of "applied steps" (each an M
expression over the previous step). Here every step is a declarative
object which is compiled into a SQL query wrapping the previous one, so
DuckDB's optimiser can fold the whole chain into a single plan - the same
idea as Power Query "query folding".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..model.schema import DUCKDB_TYPES, DataType, Step
from .sql import SQLValidationError, expression, ident, literal

AGGREGATES = {
    "sum": "SUM",
    "average": "AVG",
    "min": "MIN",
    "max": "MAX",
    "count": "COUNT",
    "countRows": "COUNT",
    "distinctCount": "COUNT_DISTINCT",
    "median": "MEDIAN",
}

FILTER_OPERATORS = {
    "equals": "=",
    "notEquals": "<>",
    "greaterThan": ">",
    "greaterThanOrEqual": ">=",
    "lessThan": "<",
    "lessThanOrEqual": "<=",
}


class StepError(ValueError):
    pass


def _arg(step: Step, name: str, default: Any = ...) -> Any:
    extra = step.model_extra or {}
    if name in extra:
        return extra[name]
    if default is ...:
        raise StepError(f"step '{step.op}' requires '{name}'")
    return default


def _cols(names: list[str]) -> str:
    return ", ".join(ident(n) for n in names)


def _type(t: str) -> str:
    try:
        return DUCKDB_TYPES[DataType(t)]
    except ValueError as e:
        raise StepError(f"unknown data type '{t}'") from e


def _select_columns(prev: str, s: Step, ref: Callable[[str], str]) -> str:
    return f"SELECT {_cols(_arg(s, 'columns'))} FROM ({prev}) AS _s"


def _remove_columns(prev: str, s: Step, ref: Callable[[str], str]) -> str:
    return f"SELECT * EXCLUDE ({_cols(_arg(s, 'columns'))}) FROM ({prev}) AS _s"


def _rename_columns(prev: str, s: Step, ref: Callable[[str], str]) -> str:
    mapping: dict[str, str] = _arg(s, "mapping")
    renames = ", ".join(f"{ident(a)} AS {ident(b)}" for a, b in mapping.items())
    return f"SELECT * RENAME ({renames}) FROM ({prev}) AS _s"


def _change_type(prev: str, s: Step, ref: Callable[[str], str]) -> str:
    types: dict[str, str] = _arg(s, "types")
    repl = ", ".join(f"TRY_CAST({ident(c)} AS {_type(t)}) AS {ident(c)}" for c, t in types.items())
    return f"SELECT * REPLACE ({repl}) FROM ({prev}) AS _s"


def _filter_rows(prev: str, s: Step, ref: Callable[[str], str]) -> str:
    cond = _arg(s, "condition", None)
    if cond is not None:
        where = expression(cond)
    else:
        col = ident(_arg(s, "column"))
        op = _arg(s, "operator")
        value = _arg(s, "value", None)
        if op in FILTER_OPERATORS:
            where = f"{col} {FILTER_OPERATORS[op]} {literal(value)}"
        elif op == "in":
            where = f"{col} IN ({', '.join(literal(v) for v in value)})"
        elif op == "notIn":
            where = f"{col} NOT IN ({', '.join(literal(v) for v in value)})"
        elif op == "contains":
            where = f"contains(CAST({col} AS VARCHAR), {literal(value)})"
        elif op == "startsWith":
            where = f"starts_with(CAST({col} AS VARCHAR), {literal(value)})"
        elif op == "isNull":
            where = f"{col} IS NULL"
        elif op == "isNotNull":
            where = f"{col} IS NOT NULL"
        else:
            raise StepError(f"unknown filter operator '{op}'")
    return f"SELECT * FROM ({prev}) AS _s WHERE {where}"


def _add_column(prev: str, s: Step, ref: Callable[[str], str]) -> str:
    expr = expression(_arg(s, "expression"))
    t = _arg(s, "dataType", None)
    if t:
        expr = f"CAST({expr} AS {_type(t)})"
    return f"SELECT *, {expr} AS {ident(_arg(s, 'name'))} FROM ({prev}) AS _s"


def _replace_values(prev: str, s: Step, ref: Callable[[str], str]) -> str:
    col = ident(_arg(s, "column"))
    old, new = _arg(s, "find"), _arg(s, "replace")
    cond = f"{col} IS NULL" if old is None else f"{col} = {literal(old)}"
    return f"SELECT * REPLACE (CASE WHEN {cond} THEN {literal(new)} ELSE {col} END AS {col}) FROM ({prev}) AS _s"


def _text_transform(prev: str, s: Step, ref: Callable[[str], str]) -> str:
    fn = {"trim": "trim", "upper": "upper", "lower": "lower", "clean": "trim"}[s.op]
    repl = ", ".join(f"{fn}(CAST({ident(c)} AS VARCHAR)) AS {ident(c)}" for c in _arg(s, "columns"))
    return f"SELECT * REPLACE ({repl}) FROM ({prev}) AS _s"


def _fill_null(prev: str, s: Step, ref: Callable[[str], str]) -> str:
    col = ident(_arg(s, "column"))
    return f"SELECT * REPLACE (COALESCE({col}, {literal(_arg(s, 'value'))}) AS {col}) FROM ({prev}) AS _s"


def _remove_duplicates(prev: str, s: Step, ref: Callable[[str], str]) -> str:
    cols = _arg(s, "columns", None)
    if not cols:
        return f"SELECT DISTINCT * FROM ({prev}) AS _s"
    return f"SELECT * FROM ({prev}) AS _s QUALIFY row_number() OVER (PARTITION BY {_cols(cols)}) = 1"


def _sort(prev: str, s: Step, ref: Callable[[str], str]) -> str:
    keys = []
    for k in _arg(s, "by"):
        if isinstance(k, str):
            k = {"column": k}
        keys.append(f"{ident(k['column'])} {'DESC' if k.get('descending') else 'ASC'}")
    return f"SELECT * FROM ({prev}) AS _s ORDER BY {', '.join(keys)}"


def _keep_top(prev: str, s: Step, ref: Callable[[str], str]) -> str:
    return f"SELECT * FROM ({prev}) AS _s LIMIT {int(_arg(s, 'count'))}"


def _group_by(prev: str, s: Step, ref: Callable[[str], str]) -> str:
    keys = _arg(s, "keys")
    parts = [ident(k) for k in keys]
    for a in _arg(s, "aggregations"):
        op = a["op"]
        if op not in AGGREGATES:
            raise StepError(f"unknown aggregation '{op}'")
        if op == "countRows":
            e = "COUNT(*)"
        elif op == "distinctCount":
            e = f"COUNT(DISTINCT {ident(a['column'])})"
        else:
            e = f"{AGGREGATES[op]}({ident(a['column'])})"
        parts.append(f"{e} AS {ident(a['name'])}")
    group = f" GROUP BY {_cols(keys)}" if keys else ""
    return f"SELECT {', '.join(parts)} FROM ({prev}) AS _s{group}"


def _merge(prev: str, s: Step, ref: Callable[[str], str]) -> str:
    kind = {"left": "LEFT", "inner": "INNER", "right": "RIGHT", "full": "FULL", "leftAnti": "ANTI", "leftSemi": "SEMI"}[
        _arg(s, "kind", "left")
    ]
    left_on, right_on = _arg(s, "leftOn"), _arg(s, "rightOn")
    if isinstance(left_on, str):
        left_on, right_on = [left_on], [right_on]
    on = " AND ".join(f"_s.{ident(a)} = _r.{ident(b)}" for a, b in zip(left_on, right_on, strict=True))
    if kind in ("ANTI", "SEMI"):
        return f"SELECT _s.* FROM ({prev}) AS _s {kind} JOIN {ref(_arg(s, 'table'))} AS _r ON {on}"
    expand: dict[str, str] | list[str] = _arg(s, "expand", [])
    if isinstance(expand, list):
        expand = {c: c for c in expand}
    extra = "".join(f", _r.{ident(c)} AS {ident(alias)}" for c, alias in expand.items())
    return f"SELECT _s.*{extra} FROM ({prev}) AS _s {kind} JOIN {ref(_arg(s, 'table'))} AS _r ON {on}"


def _append(prev: str, s: Step, ref: Callable[[str], str]) -> str:
    parts = [f"SELECT * FROM ({prev}) AS _s"] + [f"SELECT * FROM {ref(t)}" for t in _arg(s, "tables")]
    return " UNION ALL BY NAME ".join(parts)


def _unpivot(prev: str, s: Step, ref: Callable[[str], str]) -> str:
    return (
        f"UNPIVOT ({prev}) ON {_cols(_arg(s, 'columns'))} "
        f"INTO NAME {ident(_arg(s, 'attribute', 'Attribute'))} VALUE {ident(_arg(s, 'value', 'Value'))}"
    )


def _pivot(prev: str, s: Step, ref: Callable[[str], str]) -> str:
    agg = AGGREGATES.get(_arg(s, "aggregation", "sum"))
    if agg is None:
        raise StepError("unknown pivot aggregation")
    return f"PIVOT ({prev}) ON {ident(_arg(s, 'column'))} USING {agg}({ident(_arg(s, 'value'))})"


def _split_column(prev: str, s: Step, ref: Callable[[str], str]) -> str:
    col = _arg(s, "column")
    into = _arg(s, "into")
    delim = literal(_arg(s, "delimiter", ","))
    parts = ", ".join(
        f"string_split(CAST({ident(col)} AS VARCHAR), {delim})[{i + 1}] AS {ident(n)}" for i, n in enumerate(into)
    )
    return f"SELECT * EXCLUDE ({ident(col)}), {parts} FROM ({prev}) AS _s"


def _custom_sql(prev: str, s: Step, ref: Callable[[str], str]) -> str:
    # Escape hatch equivalent to Value.NativeQuery: `_prev` refers to the previous step.
    from .sql import select_query

    sql = select_query(_arg(s, "query"))
    return f"WITH _prev AS ({prev}) {sql}"


STEPS: dict[str, Callable[[str, Step, Callable[[str], str]], str]] = {
    "selectColumns": _select_columns,
    "removeColumns": _remove_columns,
    "renameColumns": _rename_columns,
    "changeType": _change_type,
    "filterRows": _filter_rows,
    "addColumn": _add_column,
    "replaceValues": _replace_values,
    "trim": _text_transform,
    "clean": _text_transform,
    "upper": _text_transform,
    "lower": _text_transform,
    "fillNull": _fill_null,
    "removeDuplicates": _remove_duplicates,
    "sort": _sort,
    "keepTop": _keep_top,
    "groupBy": _group_by,
    "merge": _merge,
    "append": _append,
    "unpivot": _unpivot,
    "pivot": _pivot,
    "splitColumn": _split_column,
    "nativeQuery": _custom_sql,
}


def compile_steps(source_sql: str, steps: list[Step], ref: Callable[[str], str]) -> str:
    """Fold ``steps`` over ``source_sql``. ``ref`` resolves other query names."""
    sql = source_sql
    for i, step in enumerate(steps):
        fn = STEPS.get(step.op)
        if fn is None:
            raise StepError(f"step {i + 1}: unknown operation '{step.op}'")
        try:
            sql = fn(sql, step, ref)
        except (StepError, SQLValidationError) as e:
            raise StepError(f"step {i + 1} ({step.op}): {e}") from e
    return sql


def step_dependencies(steps: list[Step]) -> set[str]:
    deps: set[str] = set()
    for s in steps:
        extra = s.model_extra or {}
        if s.op == "merge" and "table" in extra:
            deps.add(extra["table"])
        if s.op == "append":
            deps.update(extra.get("tables", []))
    return deps
