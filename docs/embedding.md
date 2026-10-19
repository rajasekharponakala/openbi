# Embedding reports

Embedding follows the Power BI "app owns data" pattern:

1. Your back end, signed in as an Admin or Member of the workspace, asks
   OpenBI for an embed token. If the dataset uses row-level security, it
   passes an effective identity.

   ```bash
   curl -X POST https://bi.example.com/api/reports/1/embed-token \
     -H "Authorization: Bearer $USER_TOKEN" -H 'Content-Type: application/json' \
     -d '{"roles": ["Regional Manager"], "username": "avery@example.com", "lifetimeMinutes": 60}'
   # → {"token": "...", "embedUrl": "/embed/report/1", "expiresInMinutes": 60}
   ```

   The token can read only that report and its dataset. It is subject to the
   given RLS roles and can edit the report only with `"allowEdit": true`.

2. The page embeds the report with the SDK:

   ```html
   <div id="report" style="height:600px"></div>
   <script src="https://bi.example.com/static/vendor/penpal.min.js"></script>
   <script src="https://bi.example.com/static/openbi-embed.js"></script>
   <script>
     const report = openbi.embed(document.getElementById("report"), {
       embedUrl: "https://bi.example.com/embed/report/1",
       accessToken: TOKEN,
       settings: { filterPaneEnabled: false, navContentPaneEnabled: true },
       pageName: "overview",
       filters: [openbi.models.basicFilter("Date", "Year", [2025])],
     });
     report.on("loaded", () => console.log("ready"));
     report.on("dataSelected", (e) => console.log(e.detail.filters));
   </script>
   ```

   The token is sent to the iframe over postMessage after the Penpal
   handshake. It never appears in a URL.

## API

| Method | Description |
|---|---|
| `getPages()` | `[{name, displayName, isActive}]` |
| `setPage(name)` | switch page |
| `getFilters()` / `setFilters(filters)` / `removeFilters()` | report-level filters in Power BI filter JSON |
| `getSlicerState()` / `setSlicerState(visualName, values)` | slicer selections |
| `reload()`, `fullscreen()`, `destroy()` | |
| `on(event, fn)` / `off(event, fn)` | events: `loaded`, `rendered`, `pageChanged`, `dataSelected`, `filtersApplied`, `visualRendered`, `exportDataCompleted`, `saved`, `error` |

`/static/embed-example.html` on a running server is an interactive example.
