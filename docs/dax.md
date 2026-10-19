# DAX support

OpenBI parses a practical subset of DAX and compiles it to DuckDB SQL.
Measures run with filter-context semantics. Calculated columns and RLS
filters run in row context.

## Syntax

- References: `'Table Name'[Column]`, `Table[Column]`, `[Measure]`
- Literals: numbers, `"strings"` (with `""` escapes), `TRUE()`, `FALSE()`, `BLANK()`
- Operators: `+ - * / ^`, `&` (text concatenation), `= == <> < <= > >=`,
  `&& ||`, `NOT`, `IN { ... }`
- `VAR name = expr ... RETURN expr`. A variable is evaluated in the context
  where it is defined.
- Comments: `--`, `//`, `/* */`

## Measure (filter-context) functions

| Function | Notes |
|---|---|
| `SUM AVERAGE MIN MAX COUNT COUNTA DISTINCTCOUNT DISTINCTCOUNTNOBLANK MEDIAN` | over a column |
| `COUNTROWS(table)`, `COUNTROWS(FILTER(table, cond))`, `COUNTBLANK(col)` | |
| `SUMX AVERAGEX MINX MAXX COUNTX MEDIANX (table, row-expr)` | the table may be `T`, `FILTER(T, cond)` or `ALL(T)`; the row expression may use `RELATED()` |
| `CALCULATE(expr, modifiers...)` | modifiers: a boolean filter (replaces filters on its columns), `FILTER(T, cond)` (adds), `KEEPFILTERS(cond)`, `ALL(T)` / `ALL(T[c], ...)` / `ALL()`, `REMOVEFILTERS(...)`, `ALLEXCEPT(T, T[c]...)`, `ALLSELECTED(...)` (approximation: removes grouping, keeps filters) |
| `SELECTEDVALUE(col [, alt])`, `HASONEVALUE(col)`, `ISFILTERED(col)` (approximation) | |
| `DIVIDE(a, b [, alt])` | |

`ALL(Table)` removes filters from the *expanded* table: the table plus every
table it reaches through many-to-one relationships. DAX does the same. RLS
filters are never removed.

## Scalar functions (both contexts)

`IF`, `SWITCH` (including `SWITCH(TRUE(), ...)`), `AND`, `OR`, `NOT`,
`ISBLANK`, `COALESCE`, `ABS`, `ROUND`, `INT`, `TRUNC`, `MOD`, `POWER`, `SQRT`,
`EXP`, `LN`, `MIN(a,b)`, `MAX(a,b)`, `VALUE`, `UPPER`, `LOWER`, `TRIM`, `LEN`,
`LEFT`, `RIGHT`, `MID`, `CONCATENATE`, `CONTAINSSTRING`, `FORMAT` (date
patterns and common numeric patterns), `YEAR`, `MONTH`, `DAY`, `QUARTER`,
`HOUR`, `MINUTE`, `WEEKNUM`, `WEEKDAY`, `DATE`, `TODAY`, `NOW`, `EOMONTH`,
`DATEDIFF`, `USERPRINCIPALNAME`, `USERNAME`, `RELATED` (row context).

## BLANK handling

- `+` and `-` treat a single BLANK operand as 0, but `BLANK() + BLANK()` is BLANK.
- Counting functions return BLANK, not 0, for an empty set.
- A visual drops rows where every measure is BLANK, as `SUMMARIZECOLUMNS` does.

## Not yet supported

- Context transition: a measure referenced inside an iterator or a calculated
  column, e.g. `SUMX(VALUES(Customer[Id]), [Sales])`.
- Time intelligence: `TOTALYTD`, `SAMEPERIODLASTYEAR`, `DATEADD`, … The
  workaround is a relative-date filter, or explicit measures built on
  date-table columns (e.g. `CALCULATE([Sales], 'Date'[Year] = 2024)`).
- `USERELATIONSHIP`, `CROSSFILTER`, `TREATAS`, `LOOKUPVALUE`, `RANKX`,
  table-valued results, calculation groups, hierarchies.

If an expression uses one of these, OpenBI returns a clear error instead of
silently giving a different result.
