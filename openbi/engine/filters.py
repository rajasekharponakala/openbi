"""Translate Power BI-style filter JSON into engine predicates.

The JSON shape follows the public ``powerbi-models`` contract used by the
Power BI JavaScript SDK (see docs/power-bi-analysis.md):

* Basic     ``{"filterType": 1, "target": {...}, "operator": "In", "values": [...]}``
* Advanced  ``{"filterType": 0, "target": {...}, "logicalOperator": "And", "conditions": [...]}``
* RelativeDate ``{"filterType": 4, "operator": 0, "timeUnitsCount": 3, "timeUnitType": 3, "includeToday": true}``
* TopN      ``{"filterType": 5, "operator": "Top", "itemCount": 5, "orderBy": {"table": ..., "measure": ...}}``

Targets are ``{"table": "T", "column": "C"}`` or ``{"table": "T", "measure": "M"}``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from ..dax.compiler import Predicate, col_sql
from ..powerquery.sql import literal

FILTER_ADVANCED = 0
FILTER_BASIC = 1
FILTER_RELATIVE_DATE = 4
FILTER_TOPN = 5
FILTER_TUPLE = 6

_SCHEMA_TYPES = {
    "http://powerbi.com/product/schema#advanced": FILTER_ADVANCED,
    "http://powerbi.com/product/schema#basic": FILTER_BASIC,
    "http://powerbi.com/product/schema#relativeDate": FILTER_RELATIVE_DATE,
    "http://powerbi.com/product/schema#topN": FILTER_TOPN,
    "http://powerbi.com/product/schema#tuple": FILTER_TUPLE,
}


class FilterError(ValueError):
    pass


@dataclass
class MeasureFilter:
    measure: str
    sql: str  # condition with {m} placeholder for the measure value


@dataclass
class TopNFilter:
    table: str
    column: str
    operator: str  # Top | Bottom
    count: int
    order_measure: str | None
    order_expression: str | None


@dataclass
class ParsedFilters:
    predicates: list[Predicate]
    measure_filters: list[MeasureFilter]
    topn: list[TopNFilter]


def _filter_type(f: dict[str, Any]) -> int:
    if "filterType" in f:
        return int(f["filterType"])
    schema = f.get("$schema")
    if schema in _SCHEMA_TYPES:
        return _SCHEMA_TYPES[schema]
    if "conditions" in f:
        return FILTER_ADVANCED
    if "itemCount" in f:
        return FILTER_TOPN
    if "timeUnitType" in f:
        return FILTER_RELATIVE_DATE
    return FILTER_BASIC


def _condition(x: str, cond: dict[str, Any]) -> str | None:
    op = cond.get("operator", "None")
    v = cond.get("value")
    s = f"CAST({x} AS VARCHAR)"
    match op:
        case "None":
            return None
        case "LessThan":
            return f"{x} < {literal(v)}"
        case "LessThanOrEqual":
            return f"{x} <= {literal(v)}"
        case "GreaterThan":
            return f"{x} > {literal(v)}"
        case "GreaterThanOrEqual":
            return f"{x} >= {literal(v)}"
        case "Is":
            return f"{x} = {literal(v)}"
        case "IsNot":
            return f"{x} IS DISTINCT FROM {literal(v)}"
        case "Contains":
            return f"contains(lower({s}), lower({literal(v)}))"
        case "DoesNotContain":
            return f"NOT contains(lower(COALESCE({s}, '')), lower({literal(v)}))"
        case "StartsWith":
            return f"starts_with(lower({s}), lower({literal(v)}))"
        case "DoesNotStartWith":
            return f"NOT starts_with(lower(COALESCE({s}, '')), lower({literal(v)}))"
        case "IsBlank":
            return f"{x} IS NULL"
        case "IsNotBlank":
            return f"{x} IS NOT NULL"
        case "IsEmptyString":
            return f"{s} = ''"
        case "IsNotEmptyString":
            return f"{s} <> ''"
    raise FilterError(f"unknown advanced filter operator '{op}'")


def _advanced(x: str, f: dict[str, Any]) -> str | None:
    logical = " OR " if f.get("logicalOperator", "And") == "Or" else " AND "
    parts = [p for p in (_condition(x, c) for c in f.get("conditions", [])) if p]
    if not parts:
        return None
    return "(" + logical.join(f"({p})" for p in parts) + ")"


def _basic(x: str, f: dict[str, Any]) -> str | None:
    op = f.get("operator", "In")
    if op == "All":
        return None
    values = f.get("values", [])
    if f.get("requireSingleSelection") and len(values) > 1:
        values = values[:1]
    non_null = [v for v in values if v is not None]
    has_null = len(non_null) != len(values)
    parts = []
    if non_null:
        parts.append(f"{x} IN ({', '.join(literal(v) for v in non_null)})")
    if has_null:
        parts.append(f"{x} IS NULL")
    cond = "(" + " OR ".join(parts) + ")" if parts else "FALSE"
    if op == "NotIn":
        return f"(NOT COALESCE({cond}, FALSE))"
    if op != "In":
        raise FilterError(f"unknown basic filter operator '{op}'")
    return cond


_UNITS = {0: "day", 1: "week", 2: "week", 3: "month", 4: "month", 5: "year", 6: "year"}
_CALENDAR = {2, 4, 6}


def _relative_date(x: str, f: dict[str, Any], today: date | None = None) -> str:
    today = today or date.today()
    op = f.get("operator", 0)
    n = int(f.get("timeUnitsCount", 1))
    unit_type = int(f.get("timeUnitType", 0))
    unit = _UNITS.get(unit_type)
    if unit is None:
        raise FilterError("relative time units (hours/minutes) are not supported for date filters")
    include_today = bool(f.get("includeToday", True))
    t = literal(today.isoformat())
    day = f"CAST({x} AS DATE)"
    if op == 1:  # InThis: the current calendar period
        start = f"date_trunc('{unit}', DATE {t})"
        return f"({day} >= {start} AND {day} < {start} + INTERVAL 1 {unit})"
    calendar = unit_type in _CALENDAR or unit == "day"
    if op == 0:  # InLast
        if calendar and unit != "day":
            end = f"date_trunc('{unit}', DATE {t})"
            return f"({day} >= {end} - INTERVAL {n} {unit} AND {day} < {end})"
        end = f"DATE {t} + INTERVAL 1 day" if include_today else f"DATE {t}"
        start = f"DATE {t} - INTERVAL {n} {unit}" + (" + INTERVAL 1 day" if include_today else "")
        return f"({day} >= {start} AND {day} < {end})"
    if op == 2:  # InNext
        if calendar and unit != "day":
            start = f"date_trunc('{unit}', DATE {t}) + INTERVAL 1 {unit}"
            return f"({day} >= {start} AND {day} < {start} + INTERVAL {n} {unit})"
        start = f"DATE {t}" if include_today else f"DATE {t} + INTERVAL 1 day"
        return f"({day} >= {start} AND {day} < {start} + INTERVAL {n} {unit})"
    raise FilterError(f"unknown relative date operator '{op}'")


def _resolve(graph_canonical, target: dict[str, Any]) -> tuple[str, str | None, str | None]:
    if "table" not in target:
        raise FilterError("filter target requires 'table'")
    table = graph_canonical(target["table"])
    return table, target.get("column"), target.get("measure")


def parse_filters(filters: list[dict[str, Any]], canonical, today: date | None = None) -> ParsedFilters:
    """``canonical`` maps a table name to its canonical spelling (or raises)."""
    preds: list[Predicate] = []
    mfilters: list[MeasureFilter] = []
    topn: list[TopNFilter] = []
    for f in filters or []:
        ftype = _filter_type(f)
        if ftype == FILTER_TUPLE:
            targets = f.get("target", [])
            ors = []
            cols = set()
            for tup in f.get("values", []):
                ands = []
                for tgt, val in zip(targets, tup, strict=True):
                    table, column, _ = _resolve(canonical, tgt)
                    cols.add((table, column))
                    ands.append(
                        f"{col_sql(table, column)} = {literal(val.get('value') if isinstance(val, dict) else val)}"
                    )
                ors.append("(" + " AND ".join(ands) + ")")
            tables = {c[0] for c in cols}
            if len(tables) != 1:
                raise FilterError("tuple filters must target columns of a single table")
            preds.append(Predicate(tables.pop(), frozenset(cols), "(" + (" OR ".join(ors) or "FALSE") + ")"))
            continue
        table, column, measure = _resolve(canonical, f.get("target", {}))
        if ftype == FILTER_TOPN:
            order = f.get("orderBy", {})
            topn.append(
                TopNFilter(
                    table,
                    column or "",
                    f.get("operator", "Top"),
                    int(f.get("itemCount", 10)),
                    order.get("measure"),
                    order.get("expression"),
                )
            )
            continue
        if measure:
            sql = _advanced("{m}", f) if ftype == FILTER_ADVANCED else _basic("{m}", f)
            if sql:
                mfilters.append(MeasureFilter(measure, sql))
            continue
        if not column:
            raise FilterError("filter target requires 'column' or 'measure'")
        x = col_sql(table, column)
        if ftype == FILTER_ADVANCED:
            sql = _advanced(x, f)
        elif ftype == FILTER_BASIC:
            sql = _basic(x, f)
        elif ftype == FILTER_RELATIVE_DATE:
            sql = _relative_date(x, f, today)
        else:
            raise FilterError(f"filter type {ftype} is not supported")
        if sql:
            preds.append(Predicate(table, frozenset({(table, column)}), sql))
    return ParsedFilters(preds, mfilters, topn)
