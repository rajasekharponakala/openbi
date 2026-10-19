/*
 * OpenBI embedding SDK (host page side).
 *
 * Mirrors the shape of the Power BI JavaScript SDK: embed a report into an
 * iframe with an embed token, listen to events and drive it with
 * page/filter APIs. Messaging uses Penpal (postMessage RPC), which must be
 * loaded first:  <script src="/static/vendor/penpal.min.js"></script>
 *
 *   const report = openbi.embed(el, { embedUrl, accessToken, settings, pageName, filters });
 *   report.on("loaded" | "rendered" | "pageChanged" | "dataSelected" | "filtersApplied" | "visualRendered" | "error", fn);
 *   await report.getPages();  await report.setPage("products");
 *   await report.setFilters([{ $schema, filterType: 1, target: {table, column}, operator: "In", values: [...] }]);
 */
(function (global) {
  "use strict";
  var EVENTS = ["loaded", "saved", "rendered", "error", "dataSelected", "pageChanged", "filtersApplied", "visualRendered", "exportDataCompleted"];

  function Report(container, config) {
    if (!global.Penpal) throw new Error("openbi-embed.js requires Penpal (load /static/vendor/penpal.min.js first)");
    if (!config || !config.embedUrl || !config.accessToken) throw new Error("embedUrl and accessToken are required");
    var self = this;
    this.handlers = {};
    this.iframe = document.createElement("iframe");
    this.iframe.src = config.embedUrl;
    this.iframe.style.cssText = "width:100%;height:100%;border:0";
    this.iframe.setAttribute("allowfullscreen", "true");
    container.innerHTML = "";
    container.appendChild(this.iframe);
    var renderedTimer = null;
    this.connection = global.Penpal.connectToChild({
      iframe: this.iframe,
      methods: {
        getConfig: function () {
          return { accessToken: config.accessToken, settings: config.settings || {}, pageName: config.pageName || null, filters: config.filters || [] };
        },
        emit: function (event, data) {
          self._fire(event, data);
          if (event === "visualRendered") {
            clearTimeout(renderedTimer);
            renderedTimer = setTimeout(function () { self._fire("rendered", {}); }, 150);
          }
        },
      },
    });
    this.ready = this.connection.promise;
  }

  Report.prototype._fire = function (event, data) {
    (this.handlers[event] || []).slice().forEach(function (fn) {
      try { fn({ type: event, detail: data }); } catch (e) { setTimeout(function () { throw e; }); }
    });
  };
  Report.prototype.on = function (event, fn) {
    if (EVENTS.indexOf(event) < 0) throw new Error("unknown event '" + event + "'. Allowed: " + EVENTS.join(", "));
    (this.handlers[event] = this.handlers[event] || []).push(fn);
    return this;
  };
  Report.prototype.off = function (event, fn) {
    this.handlers[event] = (this.handlers[event] || []).filter(function (h) { return fn && h !== fn; });
    return this;
  };
  function call(name) {
    return function () {
      var args = arguments;
      return this.ready.then(function (child) { return child[name].apply(null, args); });
    };
  }
  Report.prototype.getPages = call("getPages");
  Report.prototype.setPage = call("setPage");
  Report.prototype.getFilters = call("getFilters");
  Report.prototype.setFilters = call("setFilters");
  Report.prototype.removeFilters = function () { return this.setFilters([]); };
  Report.prototype.getSlicerState = call("getSlicerState");
  Report.prototype.setSlicerState = call("setSlicerState");
  Report.prototype.reload = call("reload");
  Report.prototype.fullscreen = function () { if (this.iframe.requestFullscreen) this.iframe.requestFullscreen(); };
  Report.prototype.destroy = function () { this.connection.destroy(); this.iframe.remove(); };

  global.openbi = {
    embed: function (container, config) { return new Report(container, config); },
    models: {
      FilterType: { Advanced: 0, Basic: 1, Unknown: 2, IncludeExclude: 3, RelativeDate: 4, TopN: 5, Tuple: 6, RelativeTime: 7, Identity: 8 },
      basicFilter: function (table, column, values, operator) {
        return { $schema: "http://powerbi.com/product/schema#basic", filterType: 1, target: { table: table, column: column }, operator: operator || "In", values: values };
      },
      advancedFilter: function (table, column, logicalOperator, conditions) {
        return { $schema: "http://powerbi.com/product/schema#advanced", filterType: 0, target: { table: table, column: column }, logicalOperator: logicalOperator || "And", conditions: conditions };
      },
    },
  };
})(window);
