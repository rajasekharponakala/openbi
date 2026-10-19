// Runs inside the embedding iframe: receives the embed token from the host
// page over Penpal and exposes the report API back to it.
import { setToken } from "./api.js";
import { ReportView } from "./report.js";

/* global Penpal */

const root = document.getElementById("main");
const reportId = Number(location.pathname.split("/").pop());
let view = null;

const connection = Penpal.connectToParent({
  methods: {
    getPages: () => view.getPages(),
    setPage: (name) => { view.setPage(name); return true; },
    getFilters: () => view.getFilters(),
    setFilters: (filters) => { view.setFilters(filters); return true; },
    getSlicerState: () => view.getSlicerState(),
    setSlicerState: (name, values) => { view.setSlicerState(name, values); return true; },
    reload: async () => { view.destroy(); await start(); return true; },
  },
});

let parent = null;
let config = null;

async function start() {
  view = new ReportView(root, {
    reportId,
    embed: true,
    settings: config.settings,
    onEvent: (event, data) => parent.emit(event, JSON.parse(JSON.stringify(data ?? {}))),
  });
  if (config.filters?.length) view.extraFilters = config.filters;
  await view.load();
  if (config.pageName) view.setPage(config.pageName);
}

connection.promise.then(async (p) => {
  parent = p;
  config = await p.getConfig();
  setToken(config.accessToken, false);
  try {
    await start();
  } catch (e) {
    root.innerHTML = `<div style="padding:20px;color:#c4314b">${String(e.message).replace(/</g, "&lt;")}</div>`;
    parent.emit("error", { message: e.message });
  }
});
