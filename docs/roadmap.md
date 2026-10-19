# Roadmap and known gaps

OpenBI covers the core Power BI workflow: get data → transform → model →
measures → report → share → secure → refresh → embed. The list below is what
is still missing, roughly in priority order.

## Engine / DAX
- Time intelligence (`TOTALYTD`, `SAMEPERIODLASTYEAR`, `DATEADD`, `DATESINPERIOD`) using a marked date table
- Context transition (measures inside iterators and in calculated columns), `RANKX`, `TOPN` as a table function
- `USERELATIONSHIP` / inactive relationships, `TREATAS`, many-to-many relationships
- Hierarchies with drill-down and drill-through
- Calculation groups and field parameters
- An aggregation cache for repeated visual queries

## Power Query
- A visual step editor. Steps are currently edited as JSON, with live preview
- Parsing M expressions, to import `.pbix` / PBIP queries
- Incremental refresh (range-partitioned import)
- An on-premises data gateway equivalent (an agent that runs refreshes next to private sources)

## Reports
- Bookmarks, tooltips pages, drill-through pages, conditional formatting, small multiples
- Maps (e.g. MapLibre GL, BSD-3), KPI and decomposition-tree visuals
- Custom visual SDK compatible with the `powerbi-visuals-api` `IVisual.update(DataView)` contract
- Mobile layout, themes editor, undo/redo, "sync slicers" across pages
- Natural-language Q&A (pluggable LLM)

## Service
- OIDC / SAML single sign-on (e.g. via Authlib), groups
- Apps (packaged audiences), subscriptions / e-mail snapshots, alerts on dashboard tiles
- Import of `.pbix` / PBIP (TMDL) models
- Usage metrics and audit log, deployment pipelines, Git integration from the UI
- A horizontally scaled refresh worker pool (the current scheduler runs in-process)
