(function () {
  "use strict";
  const UI = window.MultiagentUI;
  const $ = (id) => document.getElementById(id);
  const esc = UI.escapeHtml;

  function setBusy(btn, busy, label) {
    if (!btn) return;
    btn.disabled = !!busy;
    if (busy) {
      btn.dataset.prev = btn.textContent;
      btn.textContent = label || "…";
    } else if (btn.dataset.prev) {
      btn.textContent = btn.dataset.prev;
      delete btn.dataset.prev;
    }
  }

  function showResult(elId, data) {
    const el = $(elId);
    if (!el) return;
    el.style.display = "block";
    el.textContent = JSON.stringify(data, null, 2);
  }

  function showError(msg) {
    UI.showBanner("dash-error", msg || "");
  }

  // ---------- 1. Crawl ----------
  let lastJobId = null;
  $("cr-run").addEventListener("click", async () => {
    showError("");
    const body = {
      base_url: $("cr-base-url").value.trim(),
      max_services: Number($("cr-max-services").value),
      max_clinics: Number($("cr-max-clinics").value),
      max_doctor_profiles: Number($("cr-max-doctors").value),
      fetch_clinics: $("cr-fetch-clinics").checked,
      full_reviews: $("cr-full-reviews").checked,
      dual_review_pages: $("cr-dual-reviews").checked,
      discover_category_hubs: $("cr-discover-hubs").checked,
      save_to_db: $("cr-save-db").checked,
      run_in_background: $("cr-bg").checked,
    };
    setBusy($("cr-run"), true, "Запускаем…");
    try {
      const data = await UI.api("POST", "/docdoc/crawl", body);
      showResult("cr-result", data);
      if (data.job_id) {
        lastJobId = data.job_id;
        $("cr-poll").disabled = false;
      }
    } catch (e) {
      showError(e.message);
    } finally {
      setBusy($("cr-run"), false);
    }
  });

  $("cr-poll").addEventListener("click", async () => {
    if (!lastJobId) return;
    setBusy($("cr-poll"), true, "Опрашиваем…");
    try {
      const data = await UI.api("GET", "/docdoc/crawl/jobs/" + encodeURIComponent(lastJobId));
      showResult("cr-result", data);
      if (data.checkpoint_path) $("ing-path").value = data.checkpoint_path;
    } catch (e) {
      showError(e.message);
    } finally {
      setBusy($("cr-poll"), false);
    }
  });

  // ---------- 2. Ingest ----------
  $("ing-run").addEventListener("click", async () => {
    showError("");
    const body = {
      path: $("ing-path").value.trim(),
      allow_partial: $("ing-allow-partial").checked,
    };
    setBusy($("ing-run"), true, "Загружаем…");
    try {
      const data = await UI.api("POST", "/docdoc/ingest-checkpoint", body);
      showResult("ing-result", data);
      refreshCatalog();
    } catch (e) {
      showError(e.message);
    } finally {
      setBusy($("ing-run"), false);
    }
  });

  // ---------- 3. RAG build ----------
  $("rb-run").addEventListener("click", async () => {
    showError("");
    const kinds = [];
    if ($("rb-kind-review").checked) kinds.push("review");
    if ($("rb-kind-doctor").checked) kinds.push("doctor");
    if ($("rb-kind-service").checked) kinds.push("service");
    const body = {
      source: $("rb-source").value,
      crawl_path: $("rb-crawl-path").value.trim() || null,
      city_slug: $("rb-city").value.trim() || null,
      source_id: $("rb-source-id").value ? Number($("rb-source-id").value) : null,
      kinds: kinds.length ? kinds : null,
    };
    setBusy($("rb-run"), true, "Индексируем…");
    try {
      const data = await UI.api("POST", "/docdoc/rag/build", body);
      showResult("rb-result", data);
    } catch (e) {
      showError(e.message);
    } finally {
      setBusy($("rb-run"), false);
    }
  });

  // ---------- Catalog table ----------

  let currentCategory = "clinic";
  let currentItems = [];

  function renderTable(items) {
    if (!items.length) {
      return '<p class="muted" style="margin:0">Пусто. Запустите краул и постройте индекс.</p>';
    }
    const rows = items.map(function (it) {
      return "<tr>" +
        "<td>" + esc(it.name) + "</td>" +
        '<td class="num">' + esc(String(it.reviews_count || 0)) + "</td>" +
        "</tr>";
    }).join("");
    return (
      '<table class="data-table">' +
      "<thead><tr><th>Название</th><th class=\"num\">Отзывов</th></tr></thead>" +
      "<tbody>" + rows + "</tbody></table>"
    );
  }

  function applyFilter() {
    const q = $("cat-filter").value.trim().toLowerCase();
    const filtered = q
      ? currentItems.filter(function (e) { return (e.name || "").toLowerCase().indexOf(q) >= 0; })
      : currentItems;
    $("cat-table-wrap").innerHTML = renderTable(filtered);
  }

  async function refreshCatalog() {
    const wrap = $("cat-table-wrap");
    wrap.innerHTML = '<p class="muted" style="margin:0">Загружаем…</p>';
    Array.from(document.querySelectorAll(".chip-btn[data-cat]")).forEach(function (b) {
      b.classList.toggle("is-active", b.dataset.cat === currentCategory);
      b.style.background = b.dataset.cat === currentCategory ? "var(--accent-dim)" : "";
    });
    const { items } = await UI.loadEntitiesWithFallback(currentCategory, { limit: 50 });
    currentItems = items;
    applyFilter();
  }

  Array.from(document.querySelectorAll(".chip-btn[data-cat]")).forEach(function (b) {
    b.addEventListener("click", function () {
      currentCategory = b.dataset.cat;
      refreshCatalog();
    });
  });
  $("cat-filter").addEventListener("input", applyFilter);

  UI.whenReady(refreshCatalog);
  window.addEventListener("data-source-changed", refreshCatalog);
})();
