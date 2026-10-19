# How Power BI works and how OpenBI rebuilds it

This document records how Power BI is put together, which parts were checked
with [REA](https://github.com/morluto/rea), and the open-source component that
replaces each part in OpenBI.

## Method

Power BI Desktop is a closed Windows application, and the Power BI service is
a hosted SaaS product. Neither can be inspected in a Linux container, and
reverse-engineering their binaries would also conflict with the product
licence. The investigation therefore used two sources:

1. **Microsoft's public documentation and published formats.** The
   architecture (VertiPaq, the formula and storage engines, DirectQuery,
   Power Query/M, gateways, workspaces, RLS, embedding) is documented
   publicly. The project formats (PBIP, TMDL, PBIR) are published as text
   formats.
2. **REA static analysis of Microsoft's publicly distributed client packages**
   on npm: `powerbi-client` (the embedding SDK), `powerbi-models` (filter,
   page and visual contracts) and `powerbi-visuals-api` (the custom-visual
   contract). These are the real interfaces between a host page and a Power BI
   report.

   REA's `analyze-javascript-application` ran over each package without
   executing it. The output is evidence records with content digests: 128k AST
   nodes were visited in `powerbi-client` and 60k in `powerbi-models`, with no
   parse failures. [`scripts/analyze-powerbi-sdk.sh`](../scripts/analyze-powerbi-sdk.sh)
   reproduces the run. The extracted contract is in
   [`docs/rea/powerbi-sdk-findings.json`](rea/powerbi-sdk-findings.json).

### What REA showed

| Finding (from REA evidence) | What it means | OpenBI |
|---|---|---|
| `FilterType` enum: Advanced=0, Basic=1, Unknown=2, IncludeExclude=3, RelativeDate=4, TopN=5, Tuple=6, RelativeTime=7, Identity=8, Hierarchy=9 | Every filter (filter pane, slicer, cross-filter, embed API) is a JSON object with `$schema`, `target {table, column \| measure}` and a type-specific body | Same JSON contract, parsed in `openbi/engine/filters.py` (Basic, Advanced, TopN, RelativeDate, Tuple) |
| Validators: `BasicFilterValidator`, `AdvancedFilterValidator`, `TopNFilterValidator`, `RelativeDateFilterValidator`, `FilterMeasureTargetValidator`, … | Filters can target columns, hierarchies or **measures**. A filter on a measure is applied after aggregation | Measure targets become post-aggregation (HAVING-style) conditions |
| `FiltersLevel`: Report, Page, Visual | There are three filter scopes, and they combine | `report.filters`, `page.filters` and `visual.filters`, combined in `report.js#filtersFor` |
| Embed events: `loaded, rendered, error, dataSelected, pageChanged, filtersApplied, visualRendered, visualClicked, selectionChanged, exportDataCompleted, …` | The host page observes the report through postMessage events | `openbi-embed.js` emits the same event names |
| Routes: `/report/load`, `/report/pages`, `/report/filters`, `/report/bookmarks/*`, `/reports/:id/pages/:page/visuals/:visual/events/:event` | The SDK is an RPC layer over `window.postMessage` | Penpal (postMessage RPC) carries `getPages`, `setPage`, `getFilters`, `setFilters`, … |
| `TokenType`: Aad, Embed; `Permissions`: Read, ReadWrite, Copy, Create; `ViewMode`: View, Edit | Embedding uses either a user token or a scoped *embed token*, and edit rights are a token property | Embed tokens are JWTs scoped to one report, with an `edit` flag and an effective identity (username and RLS roles) |
| `DataCacheMode`: Import, DirectQuery | Each table uses one of two storage modes | `mode: import \| directQuery` per table |
| `AggregateFunction`: Sum, Average, Min, Max, Count, DistinctCount, None | Implicit measures: a numeric column dropped into a value well is aggregated | `{field, aggregate}` field references (implicit measures) |
| `VisualDataRoleKind`: Grouping, Measure, GroupingOrMeasure and `capabilities.json` (`dataRoles`, `dataViewMappings`, …) | Visuals declare *data roles* (field wells), and the engine shapes a DataView for them | Each visual type declares roles (`visuals.js#VISUALS`) and builds its semantic query from them |
| `ExportDataType`: Summarized, Underlying; format Csv/Xlsx | Users can export a visual's data | `POST /api/datasets/{id}/export` (CSV / XLSX) |

## The architecture of Power BI

```
 Sources ──► Power Query (M) ──► VertiPaq / DirectQuery ──► Formula engine (DAX) ──► Visuals
 (files, DBs,   applied steps,      columnar in-memory         filter context,         report pages,
  APIs)         query folding       store, compression          relationships, RLS       cross-filtering
                                                                   ▲
              Power BI service: workspaces, roles, refresh schedules, gateways, sharing, dashboards, embedding
```

1. **Get data / Power Query.** Each table is the result of an M query: a
   source followed by a list of applied steps such as filter, rename, change
   type, merge, append, group and unpivot. Where possible the whole step
   chain *folds* into one native query against the source.
2. **Storage.** In *Import* mode, data is loaded into VertiPaq, the columnar,
   dictionary- and run-length-compressed in-memory store of Analysis
   Services Tabular. In *DirectQuery* mode, queries go to the source at view
   time.
3. **Semantic model.** The model holds tables, columns (including calculated
   columns), measures written in DAX, relationships (cardinality and filter
   direction, active or inactive), hierarchies and row-level-security roles.
   It is serialised as TMDL in PBIP projects.
4. **Formula engine.** A visual sends a DAX query, essentially
   `SUMMARIZECOLUMNS(group-by columns, filters, measures)`. The formula engine
   evaluates each measure under a *filter context*:
   - a filter on a dimension flows to facts along one-to-many relationships,
     and back again only for relationships marked *both*;
   - `CALCULATE` changes the context. A boolean filter replaces existing
     filters on its columns, and `ALL`/`REMOVEFILTERS` remove filters,
     including from the *expanded table*;
   - iterators (`SUMX` and similar) evaluate a row expression for each row;
   - RLS filters are applied to the tables and cannot be removed by DAX.

   The storage engine runs simple scans and aggregations. The formula engine
   joins their results.
5. **Report.** A report is a set of pages with visuals on a canvas. Each
   visual has field wells (data roles), formatting and visual-level filters.
   Slicers and clicks on data points *cross-filter* the other visuals on the
   page. The filter pane shows report, page and visual filters.
6. **Service.** Workspaces have Admin, Member, Contributor and Viewer roles.
   Published datasets have scheduled refresh (through a gateway for
   on-premises data) and refresh history. Dashboards are made of tiles pinned
   from reports. Sharing and apps distribute content. Export to CSV and Excel
   is available. RLS binds only Viewers; authors can "test as role".
7. **Embedding.** A back end calls `GenerateToken` to get an embed token for a
   report, optionally with an *effective identity* (username and roles). The
   page loads `powerbi-client`, which drives the report iframe over
   postMessage.

## Component mapping

| Power BI | OpenBI | Library (see [foss-decisions.md](foss-decisions.md)) |
|---|---|---|
| Power Query applied steps and query folding | Declarative steps compiled to one SQL query (`openbi/powerquery/steps.py`) | DuckDB, SQLGlot |
| Connectors: CSV, Excel, Parquet, JSON, SQL databases | DuckDB readers, `ATTACH` for Postgres, MySQL and SQLite, openpyxl for XLSX | DuckDB, openpyxl |
| VertiPaq (Import) | A DuckDB database file per dataset, refreshed atomically | DuckDB |
| DirectQuery | DuckDB views over the source | DuckDB |
| DAX language | A parser for a DAX subset (`openbi/dax/parser.py`) | Lark |
| Formula engine | Filter-context compiler and query planner (`openbi/dax/compiler.py`, `openbi/engine/query.py`) | Custom: no FOSS DAX engine exists |
| Row-level security | DAX role filters, `USERPRINCIPALNAME()`, union of roles | — |
| PBIP / TMDL project | `openbi.yaml` + `model.yaml` + `reports/*.report.json` | PyYAML, Pydantic |
| Report canvas | Grid layout, 18 visual types, cross-filtering, slicers, filter pane, designer | Apache ECharts, gridstack.js |
| Service: workspaces, roles, refresh, history, dashboards, export | FastAPI REST API, SQL metadata store, cron refresh | FastAPI, SQLAlchemy, APScheduler, openpyxl |
| Embed tokens and `powerbi-client` | Scoped JWT embed tokens and the `openbi-embed.js` SDK | PyJWT, Penpal |

## Behaviours reproduced deliberately

- **Filter context.** A measure is evaluated per (home table, context)
  subquery and joined back on the group keys that remain. This gives the
  results DAX gives for
  `DIVIDE([Sales], CALCULATE([Sales], ALL(Sales)))` and
  `CALCULATE([Sales], Product[Category] = "Bikes")`, where the override
  replaces the row's own category. It also handles measures from two fact
  tables in one visual. `tests/test_engine.py` checks each case against
  hand-written SQL.
- **BLANK semantics.** `BLANK() + 1 = 1`, `BLANK() + BLANK() = BLANK()`,
  `COUNTROWS` of an empty set is BLANK, and rows where every measure is BLANK
  are dropped.
- **Sort by column.** For example, `Month` is sorted by `MonthNumber`.
- **RLS.** It applies to Viewers and embed tokens. Authors bypass it but can
  "view as" a role. Multiple roles combine as a union.
- **Atomic refresh.** A failed refresh keeps the previous data online and
  records the error in the refresh history.

The supported DAX subset and the known gaps are listed in
[dax.md](dax.md) and [roadmap.md](roadmap.md).
