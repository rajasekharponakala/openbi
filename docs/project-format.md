# Project format

A project is a folder of text files that can be diffed and reviewed in git,
like Power BI's PBIP. `openbi sample my-project` writes a complete example.

```
my-project/
  openbi.yaml                    # name: ..., model: model.yaml
  model.yaml                     # semantic model
  reports/sales-overview.report.json
  data/*.csv                     # optional local files referenced by sources
```

## model.yaml

```yaml
name: Sales
tables:
  - name: Sales
    mode: import                 # or directQuery
    source: {kind: csv, path: data/sales.csv}   # csv | parquet | json | excel | sql | inline | database
    steps:                       # Power Query applied steps
      - {op: changeType, types: {OrderDate: date, Quantity: int64}}
      - {op: filterRows, column: Quantity, operator: greaterThan, value: 0}
    columns:
      - {name: Line Amount, dataType: double, expression: "Sales[Quantity] * Sales[UnitPrice]"}  # calculated (DAX)
      - {name: OrderID, isHidden: true}
    measures:
      - {name: Total Sales, expression: "SUM(Sales[Line Amount])", formatString: "$#,0"}
  - name: Date
    source: {kind: sql, query: "SELECT CAST(range AS DATE) AS \"Date\" FROM range(DATE '2023-01-01', DATE '2026-01-01', INTERVAL 1 DAY)"}
    steps:
      - {op: addColumn, name: Month, expression: "strftime(\"Date\", '%b')"}
      - {op: addColumn, name: MonthNumber, expression: "month(\"Date\")", dataType: int64}
    columns:
      - {name: Month, sortByColumn: MonthNumber}
relationships:
  - {fromTable: Sales, fromColumn: OrderDate, toTable: Date, toColumn: Date}   # many → one
  # crossFilteringBehavior: both   isActive: false
roles:
  - name: Regional Manager
    tablePermissions:
      - {table: Region, filterExpression: "Region[ManagerEmail] = USERPRINCIPALNAME()"}
    members: [avery@example.com]
```

Column names are case-insensitive, as they are in DuckDB.

### Sources

| kind | fields |
|---|---|
| `csv` | `path`, `delimiter`, `header` |
| `parquet`, `json` | `path` (globs allowed) |
| `excel` | `path`, `sheet` |
| `sql` | `query`: a single read-only SELECT run by DuckDB |
| `inline` | `rows`: a list of objects |
| `database` | `connection` (`postgres:dbname=… host=…`, `mysql:…`, `sqlite:path`, `duckdb:path`) plus `table` or `query`. Disabled on the server unless `OPENBI_ALLOW_DATABASE_SOURCES=1` |

In the service, relative paths resolve against the dataset's `data/`
directory. File access is sandboxed to that directory.

### Steps

`selectColumns`, `removeColumns`, `renameColumns`, `changeType`,
`filterRows` (`condition` SQL, or `column` + `operator` + `value`),
`addColumn` (SQL `expression`), `replaceValues`, `trim`, `clean`, `upper`,
`lower`, `fillNull`, `removeDuplicates`, `sort`, `keepTop`, `groupBy`,
`merge` (join another query: left, inner, right, full, leftAnti, leftSemi),
`append`, `unpivot`, `pivot`, `splitColumn`, `nativeQuery` (`_prev` refers
to the previous step).

## Report JSON

```json
{
  "name": "Sales Overview",
  "theme": {"dataColors": ["#2E6FDB", "#F28C28"]},
  "filters": [],                                   // report level
  "pages": [{
    "name": "overview", "displayName": "Overview",
    "filters": [],                                 // page level
    "visuals": [{
      "name": "salesByCategory", "type": "donutChart",
      "x": 8, "y": 2, "w": 4, "h": 4,              // 12-column grid, 70px rows
      "title": "Sales by category",
      "fields": {"category": ["Product[Category]"], "values": ["[Total Sales]"]},
      "filters": [],                               // visual level
      "format": {"sort": "valueDesc", "showLegend": true}
    }]
  }]
}
```

Field references are `"Table[Column]"`, `"[Measure]"` or
`{"field": "Table[Column]", "aggregate": "sum"}`, which is an implicit
measure. Filters use the Power BI filter JSON. See
[how-power-bi-works.md](how-power-bi-works.md).

Visual types and their wells:

| type | wells |
|---|---|
| card | values |
| columnChart, stackedColumnChart, barChart, stackedBarChart, lineChart, areaChart | category, series, values |
| comboChart | category, values (columns), lineValues |
| pieChart, donutChart, treemap, funnel | category, values |
| scatterChart | category, x, y, size |
| gauge | values, target |
| table | columns |
| matrix | rows, columns, values |
| slicer | field |
| textbox | — (`format.text`) |
