(function () {
  "use strict";
  const UI = window.MultiagentUI;
  const TYPE_LABEL = { clinic: "Клиник", service: "Услуг", doctor: "Врачей", category: "Направлений" };

  function ru(n) { return Number(n).toLocaleString("ru-RU"); }

  async function refreshTile(type) {
    const tile = document.querySelector('.stat-tile[data-type="' + type + '"]');
    if (!tile) return null;
    const { items, source } = await UI.loadEntitiesWithFallback(type, { limit: 50 });
    const valueEl = tile.querySelector(".stat-value");
    const sampleEl = tile.querySelector(".stat-sample");
    if (!items.length) {
      valueEl.textContent = "0";
      sampleEl.textContent = "нет данных";
      return source;
    }
    const more = items.length >= 50 ? "+" : "";
    valueEl.textContent = ru(items.length) + more;
    const sample = items.slice(0, 4).map(function (e) { return e.name; }).join(" · ");
    sampleEl.textContent = "напр.: " + sample;
    return source;
  }

  async function refreshCatalog() {
    const ds = UI.DataSource.get();
    const sources = await Promise.all([
      refreshTile("clinic"),
      refreshTile("service"),
      refreshTile("doctor"),
      refreshTile("category"),
    ]);
    const empty = sources.every(function (s) { return s === "empty"; });
    document.getElementById("catalog-empty").style.display = empty ? "block" : "none";
    const usedFallback = sources.some(function (s) { return s === "json-fallback"; });
    const info = document.getElementById("catalog-source-info");
    if (!info) return;
    if (empty) {
      info.textContent = "источник: пусто";
    } else if (ds.data_source === "json" || usedFallback) {
      info.textContent = "источник: JSON " + (ds.crawl_path || "docdoc_crawl_last.json");
    } else if (ds.data_source === "db") {
      info.textContent = "источник: PostgreSQL";
    } else {
      info.textContent = "источник: PostgreSQL (auto)";
    }
  }

  fetch("/health")
    .then(function (r) { return r.json(); })
    .then(function (d) {
      const el = document.getElementById("health-line");
      if (el) el.textContent = "API: " + (d.status || "ok");
      const f = document.getElementById("footer-status");
      if (f) f.textContent = "API доступен";
    })
    .catch(function () {
      const el = document.getElementById("health-line");
      if (el) el.textContent = "API недоступен — проверьте, что сервер запущен.";
    });

  UI.whenReady(refreshCatalog);
  window.addEventListener("data-source-changed", refreshCatalog);
})();
