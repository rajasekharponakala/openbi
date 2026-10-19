# FOSS decisions

OpenBI follows the
[foss-mind](https://gitlab.com/ranjithraj/foss-agent-skills/-/blob/main/foss-mind.md)
skill: *search before you ship*. Every capability below was first matched
against an existing open-source project. Custom code is limited to the gaps
that no maintained library fills, and each gap is documented here.

## Libraries used

| Need | Library | License | Why this one | Fit |
|---|---|---|---|---|
| Columnar analytical engine (VertiPaq equivalent), CSV/Parquet/JSON readers, database attach, sandboxing | [DuckDB](https://duckdb.org) | MIT | Embedded, vectorised, very widely used, actively maintained. Its `allowed_directories` / `enable_external_access` settings give a real file-system sandbox | 100%, integrated directly |
| SQL identifier/literal quoting, expression validation | [SQLGlot](https://github.com/tobymao/sqlglot) | MIT | Full SQL parser with a DuckDB dialect; lets user-written step expressions be parsed and rejected when they contain DDL, DML or file functions | 100%, integrated directly |
| DAX parsing | [Lark](https://github.com/lark-parser/lark) | MIT | Mature parser generator. The grammar is about 50 lines (`openbi/dax/parser.py`) | Wrapper (grammar + AST) |
| Model schema and validation | [Pydantic](https://docs.pydantic.dev) | MIT | Standard | 100% |
| Project files | [PyYAML](https://pyyaml.org) | MIT | Standard | 100% |
| REST API | [FastAPI](https://fastapi.tiangolo.com) + [Uvicorn](https://www.uvicorn.org) | MIT / BSD-3 | Standard, typed, OpenAPI for free | 100% |
| Metadata store | [SQLAlchemy](https://www.sqlalchemy.org) | MIT | SQLite by default, Postgres by URL | 100% |
| Tokens | [PyJWT](https://github.com/jpadilla/pyjwt) | MIT | Standard JWT implementation | 100% |
| Password hashing | Python `hashlib.scrypt` (stdlib, OpenSSL) | PSF | Memory-hard KDF without an extra dependency | 100% |
| Scheduled refresh | [APScheduler](https://github.com/agronholm/apscheduler) 3.x | MIT | Cron triggers, coalescing, max-instances | 100% |
| Excel import/export | [openpyxl](https://foss.heptapod.net/openpyxl/openpyxl) | MIT | De facto standard | Thin adapter (XLSX → CSV → DuckDB) |
| CLI | [Typer](https://typer.tiangolo.com) | MIT | Standard | 100% |
| Charts | [Apache ECharts](https://echarts.apache.org) 5.6 | Apache-2.0 | Covers every chart type a BI tool needs (bar, line, combo, pie, treemap, funnel, scatter, gauge), with click events for cross-filtering | 100% |
| Report canvas layout (drag/resize) | [gridstack.js](https://gridstackjs.com) 11 | MIT | Drag-and-drop dashboard grid used by many BI tools | 100% |
| Embedding RPC over postMessage | [Penpal](https://github.com/Aaronius/penpal) 6 | MIT | Promise-based iframe RPC with a handshake. Replaces the hand-written postMessage router that `powerbi-client` ships | 100% |

The front-end libraries are vendored under `openbi/web/static/vendor/` with
their licences, so a deployment needs no CDN.

## Custom code, and why

| Component | Searched for | Result | Mitigation |
|---|---|---|---|
| **DAX → SQL compiler / filter-context engine** (`openbi/dax/compiler.py`, `openbi/engine/query.py`) | DAX engines for Python and JS, semantic layers (Cube, MetricFlow, Malloy, boring-semantic-layer, Ibis) | No maintained open-source DAX *evaluator* exists. Semantic layers model metrics, but none implements DAX's `CALCULATE` / expanded-table / RLS semantics | Kept small (about 1,100 lines) and generates plain SQL for DuckDB to execute. The core filter-context rules are covered by tests against hand-written SQL. A future semantic-layer library with context-transition semantics would replace it |
| **Power Query step compiler** (`openbi/powerquery/steps.py`) | M-language engines | No open-source Power Query M engine exists. dbt and SQLMesh are SQL-first project tools, not step-recipe compilers | Steps are a thin, declarative layer over SQL (one function per step, about 280 lines); DuckDB does the work |
| **Power BI filter JSON → SQL** (`openbi/engine/filters.py`) | `powerbi-models` (MIT) defines the shapes but is a TypeScript validator only | The contract is reused as is. Only the translation is new | Shapes are kept identical so host code written for Power BI's filter objects works unchanged |
| **Visual renderers** (`openbi/web/static/js/visuals.js`) | — | ECharts draws the charts; only DataView→option mapping is custom | Thin adapter |

## Licence

OpenBI is licensed **AGPL-3.0-only**. foss-mind prefers strong copyleft, and
for a network service the AGPL's network clause keeps hosted modifications
open. All dependencies are permissive (MIT, BSD or Apache-2.0) and compatible
with it.

## Review

Revisit this table every 6–12 months, as foss-mind recommends. Watch for
upstream projects that could replace the custom engine pieces. One example
would be a semantic layer that adopts filter-context semantics.
