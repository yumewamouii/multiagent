(function () {
  "use strict";

  const NAV_ITEMS = [
    { href: "/", key: "home", label: "Главная" },
    { href: "/dashboard", key: "dashboard", label: "Дашборд" },
    { href: "/reputation", key: "reputation", label: "Анализ" },
    { href: "/compare", key: "compare", label: "Сравнение" },
    { href: "/search", key: "search", label: "Поиск" },
    { href: "/chat", key: "chat", label: "Чат" },
  ];

  // -------- helpers --------

  function whenReady(cb) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", cb);
    } else {
      cb();
    }
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function el(html) {
    const t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content.firstChild;
  }

  // -------- shell --------

  function buildHeader(activeKey) {
    const desktop = NAV_ITEMS.map(function (i) {
      const cls = i.key === activeKey ? ' class="is-active"' : "";
      return '<a href="' + i.href + '" data-nav="' + i.key + '"' + cls + ">" + escapeHtml(i.label) + "</a>";
    }).join("");
    const mobile = NAV_ITEMS.map(function (i) {
      const cls = i.key === activeKey ? ' class="is-active"' : "";
      return '<a href="' + i.href + '" data-nav="' + i.key + '"' + cls + ">" + escapeHtml(i.label) + "</a>";
    }).join("");
    return [
      '<div class="header-inner">',
      '  <a href="/" class="brand" style="text-decoration:none;color:inherit">',
      '    <span class="brand-mark" aria-hidden="true">',
      '      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">',
      '        <path d="M11 3v18"/><path d="M3 11h18"/><circle cx="11" cy="11" r="9"/>',
      '      </svg>',
      '    </span>',
      '    DocDoc Reputation',
      '  </a>',
      '  <nav class="nav-desktop" aria-label="Основное меню">' + desktop + "</nav>",
      '  <button type="button" class="menu-toggle" data-menu-toggle aria-expanded="false" aria-label="Открыть меню">',
      '    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6h16M4 12h16M4 18h16"/></svg>',
      '  </button>',
      '</div>',
      '<nav class="nav-mobile" data-menu-panel aria-label="Мобильное меню">' + mobile + "</nav>",
    ].join("");
  }

  function buildFooter() {
    return [
      '<span id="footer-status">DocDoc Reputation UI</span>',
      ' · <a href="/methodology">Методика</a>',
      ' · <a href="/docs">OpenAPI</a>',
      ' · <a href="/health">/health</a>',
    ].join("");
  }

  function attachMenuToggle() {
    const toggle = document.querySelector("[data-menu-toggle]");
    const panel = document.querySelector("[data-menu-panel]");
    if (!toggle || !panel) return;
    toggle.addEventListener("click", function () {
      const open = panel.classList.toggle("is-open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    panel.querySelectorAll("a").forEach(function (a) {
      a.addEventListener("click", function () {
        panel.classList.remove("is-open");
        toggle.setAttribute("aria-expanded", "false");
      });
    });
  }

  function activeKeyFromPath() {
    const path = window.location.pathname;
    const exact = NAV_ITEMS.find(function (i) { return i.href === path; });
    return exact ? exact.key : "home";
  }

  function renderShell(opts) {
    const activeKey = (opts && opts.activeKey) || activeKeyFromPath();
    const headerEl = document.getElementById("site-header");
    if (headerEl) headerEl.innerHTML = buildHeader(activeKey);
    const footerEl = document.getElementById("site-footer");
    if (footerEl) footerEl.innerHTML = buildFooter();
    attachMenuToggle();
  }

  // -------- data-source bar --------

  const DS = {
    KEY_SOURCE: "mu_data_source",
    KEY_CRAWL: "mu_crawl_path",
    KEY_CITY: "mu_city_slug",
    get: function () {
      return {
        data_source: localStorage.getItem(this.KEY_SOURCE) || "auto",
        crawl_path: localStorage.getItem(this.KEY_CRAWL) || "docdoc_crawl_last.json",
        city_slug: localStorage.getItem(this.KEY_CITY) || "",
      };
    },
    set: function (v) {
      if (v.data_source != null) localStorage.setItem(this.KEY_SOURCE, v.data_source);
      if (v.crawl_path != null) localStorage.setItem(this.KEY_CRAWL, v.crawl_path);
      if (v.city_slug != null) localStorage.setItem(this.KEY_CITY, v.city_slug);
    },
  };

  function renderDataSourceBar() {
    const mount = document.getElementById("data-source-bar");
    if (!mount) return;
    const ds = DS.get();
    mount.innerHTML = [
      '<div class="ds-bar">',
      '  <div class="ds-bar-title">',
      '    <span class="muted">Источник данных:</span>',
      '    <select id="ds-source" class="chat-select">',
      '      <option value="auto"' + (ds.data_source === "auto" ? " selected" : "") + ">авто (БД, иначе JSON)</option>",
      '      <option value="db"' + (ds.data_source === "db" ? " selected" : "") + ">только БД</option>",
      '      <option value="json"' + (ds.data_source === "json" ? " selected" : "") + ">только JSON</option>",
      "    </select>",
      "  </div>",
      '  <div class="ds-bar-row">',
      '    <label for="ds-crawl-path" class="muted" style="font-size:.8rem">crawl_path</label>',
      '    <input id="ds-crawl-path" type="text" class="chat-input-inline" value="' + escapeHtml(ds.crawl_path) + '" placeholder="docdoc_crawl_last.json">',
      '    <label for="ds-city" class="muted" style="font-size:.8rem">city_slug</label>',
      '    <input id="ds-city" type="text" class="chat-input-inline" value="' + escapeHtml(ds.city_slug) + '" placeholder="irk">',
      '    <button type="button" class="btn btn-ghost" id="ds-apply">Применить</button>',
      "  </div>",
      "</div>",
    ].join("");
    document.getElementById("ds-apply").addEventListener("click", function () {
      DS.set({
        data_source: document.getElementById("ds-source").value,
        crawl_path: document.getElementById("ds-crawl-path").value.trim(),
        city_slug: document.getElementById("ds-city").value.trim(),
      });
      window.dispatchEvent(new CustomEvent("data-source-changed"));
    });
  }

  // -------- API --------

  async function api(method, path, body) {
    const init = { method: method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined && body !== null) init.body = JSON.stringify(body);
    const resp = await fetch(path, init);
    let payload = null;
    try { payload = await resp.json(); } catch (_) { /* not json */ }
    if (!resp.ok) {
      const detail = (payload && (payload.detail || payload.message || payload.error)) || ("HTTP " + resp.status);
      const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      err.status = resp.status;
      err.payload = payload;
      throw err;
    }
    return payload;
  }

  // -------- catalog (real entities for autocomplete) --------

  async function loadEntities(entityType, opts) {
    const ds = Object.assign({}, DS.get(), opts || {});
    const body = {
      entity_type: entityType,
      limit: (opts && opts.limit) || 50,
      use_llm: false,
      use_rag: false,
      preset: null,
      city_slug: ds.city_slug || null,
      crawl_path: ds.data_source === "db" ? null : (ds.crawl_path || null),
    };
    try {
      const data = await api("POST", "/docdoc/research/table", body);
      const rows = data.rows || [];
      return rows.map(function (r) {
        return {
          id: r.entity_id,
          name: r.entity_name,
          reviews_count: r.reviews_count != null ? r.reviews_count : (r.cells && r.cells.reviews_count) || 0,
        };
      });
    } catch (e) {
      // mute — auto-fallback
      return [];
    }
  }

  async function loadEntitiesWithFallback(entityType, opts) {
    let items = await loadEntities(entityType, opts);
    if (items.length) return { items: items, source: opts && opts.source ? opts.source : "primary" };
    // try opposite source
    const ds = DS.get();
    if (ds.data_source !== "json") {
      items = await loadEntities(entityType, { crawl_path: ds.crawl_path || "docdoc_crawl_last.json" });
      if (items.length) return { items: items, source: "json-fallback" };
    }
    return { items: [], source: "empty" };
  }

  function fillDatalist(datalistId, items) {
    const dl = document.getElementById(datalistId);
    if (!dl) return;
    dl.innerHTML = items.map(function (e) {
      const meta = e.reviews_count ? " (" + e.reviews_count + " отз.)" : "";
      return '<option value="' + escapeHtml(e.name) + '">' + escapeHtml(e.name) + escapeHtml(meta) + "</option>";
    }).join("");
  }

  // ---- input helper: clickable suggestions list ----

  function bindSuggestions(opts) {
    // opts: { mountId, inputId, onClick(name), items, label }
    const mount = document.getElementById(opts.mountId);
    if (!mount) return;
    if (!opts.items || !opts.items.length) {
      mount.innerHTML = '<p class="muted" style="margin:0;font-size:.82rem">Подсказок пока нет — проверьте источник данных или запустите краул на дашборде.</p>';
      return;
    }
    const top = opts.items.slice(0, opts.limit || 12);
    const chips = top.map(function (e) {
      return '<button type="button" class="chip-btn" data-name="' + escapeHtml(e.name) + '">' +
        escapeHtml(e.name) + (e.reviews_count ? ' <span class="muted">' + e.reviews_count + "</span>" : "") +
        "</button>";
    }).join("");
    mount.innerHTML = '<div class="suggest-grid">' + chips + "</div>";
    Array.from(mount.querySelectorAll(".chip-btn")).forEach(function (b) {
      b.addEventListener("click", function () {
        const name = b.getAttribute("data-name");
        if (opts.onClick) opts.onClick(name);
        else if (opts.inputId) {
          const inp = document.getElementById(opts.inputId);
          if (inp) {
            inp.value = name;
            inp.dispatchEvent(new Event("change"));
          }
        }
      });
    });
  }

  // -------- banners --------

  function showBanner(id, message) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = message || "";
    if (message) el.classList.add("is-visible");
    else el.classList.remove("is-visible");
  }

  // -------- expose --------

  window.MultiagentUI = {
    renderShell: renderShell,
    renderDataSourceBar: renderDataSourceBar,
    DataSource: DS,
    showBanner: showBanner,
    api: api,
    escapeHtml: escapeHtml,
    el: el,
    loadEntities: loadEntities,
    loadEntitiesWithFallback: loadEntitiesWithFallback,
    fillDatalist: fillDatalist,
    bindSuggestions: bindSuggestions,
    whenReady: whenReady,
  };

  // Any uncaught JS error becomes a visible banner so the user sees it
  // instead of silently broken UI.
  function showFatal(msg) {
    let bar = document.getElementById("__fatal-bar");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "__fatal-bar";
      bar.style.cssText =
        "position:fixed;top:0;left:0;right:0;z-index:99999;" +
        "background:#7a1530;color:#fff;padding:10px 14px;font:13px monospace;" +
        "border-bottom:1px solid #ff6b8a;";
      document.body.appendChild(bar);
    }
    bar.textContent = "JS error: " + msg + "  (откройте DevTools → Console для деталей)";
  }
  window.addEventListener("error", function (e) {
    showFatal((e.error && e.error.message) || e.message || "unknown");
  });
  window.addEventListener("unhandledrejection", function (e) {
    showFatal((e.reason && e.reason.message) || String(e.reason || "unknown"));
  });

  whenReady(function () {
    try {
      renderShell();
      renderDataSourceBar();
    } catch (e) {
      showFatal(e.message || String(e));
      throw e;
    }
  });
})();
