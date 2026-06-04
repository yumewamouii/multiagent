(function () {
  "use strict";
  const UI = window.MultiagentUI;
  const $ = (id) => document.getElementById(id);
  const esc = UI.escapeHtml;

  function collectKinds() {
    const out = [];
    if ($("srch-kind-review").checked) out.push("review");
    if ($("srch-kind-doctor").checked) out.push("doctor");
    if ($("srch-kind-service").checked) out.push("service");
    return out.length ? out : null;
  }

  function renderItem(it) {
    const meta = [];
    if (it.kind) meta.push('<span class="tag">' + esc(it.kind) + "</span>");
    if (it.score != null) meta.push('<span class="muted">score ' + Number(it.score).toFixed(3) + "</span>");
    if (it.semantic_similarity != null) meta.push('<span class="muted">sem ' + Number(it.semantic_similarity).toFixed(3) + "</span>");
    if (it.lexical_overlap != null) meta.push('<span class="muted">lex ' + Number(it.lexical_overlap).toFixed(3) + "</span>");
    if (it.rating_value != null) meta.push('<span class="tag">rating ' + esc(String(it.rating_value)) + "</span>");
    if (it.clinic_name) meta.push('<span class="tag">' + esc(it.clinic_name) + "</span>");
    if (it.service_name) meta.push('<span class="tag">' + esc(it.service_name) + "</span>");
    if (it.doctor_name) meta.push('<span class="tag">' + esc(it.doctor_name) + "</span>");

    const link = it.source_page_url
      ? ' · <a href="' + esc(it.source_page_url) + '" target="_blank" rel="noreferrer">источник</a>'
      : "";

    return (
      '<div class="snippet">' +
      '<div class="snippet-title">' + esc(it.title || "(без названия)") + link + "</div>" +
      "<div>" + esc(it.snippet || "") + "</div>" +
      '<div class="snippet-meta">' + meta.join(" ") + "</div>" +
      "</div>"
    );
  }

  // ---- preset queries ----
  Array.from(document.querySelectorAll(".chip-btn[data-q]")).forEach(function (b) {
    b.addEventListener("click", function () {
      $("srch-query").value = b.dataset.q;
      $("srch-query").focus();
    });
  });

  // ---- autocomplete для фильтров ----
  async function refreshFilterOptions() {
    const tasks = [
      ["clinic", "srch-clinic-options"],
      ["service", "srch-service-options"],
      ["category", "srch-cat-options"],
    ];
    for (const [type, listId] of tasks) {
      const { items } = await UI.loadEntitiesWithFallback(type, { limit: 50 });
      UI.fillDatalist(listId, items);
    }
  }
  UI.whenReady(refreshFilterOptions);
  window.addEventListener("data-source-changed", refreshFilterOptions);

  $("srch-run").addEventListener("click", async () => {
    UI.showBanner("srch-error", "");
    const q = $("srch-query").value.trim();
    if (q.length < 2) {
      UI.showBanner("srch-error", "Введите минимум 2 символа.");
      return;
    }
    const ds = UI.DataSource.get();
    const body = {
      query: q,
      top_k: Number($("srch-top-k").value),
      kinds: collectKinds(),
      city_slug: ($("srch-city").value.trim() || ds.city_slug) || null,
      clinic_alias: $("srch-clinic-alias").value.trim() || null,
      service_name: $("srch-service-name").value.trim() || null,
      parent_service_name: $("srch-parent-service").value.trim() || null,
      source_id: $("srch-source-id").value ? Number($("srch-source-id").value) : null,
    };

    $("srch-loading").style.display = "block";
    $("srch-result").style.display = "none";
    $("srch-run").disabled = true;

    try {
      const data = await UI.api("POST", "/docdoc/rag/search", body);
      if (!data.ok) {
        UI.showBanner("srch-error", "Ошибка: " + (data.error || "unknown"));
        return;
      }
      $("srch-result-title").textContent = "Результаты (" + (data.items ? data.items.length : 0) + ")";
      const meta = [];
      if (data.candidate_count != null) meta.push("кандидатов: " + data.candidate_count);
      if (data.top_k != null) meta.push("top_k: " + data.top_k);
      if (data.embedding_ok != null) meta.push("embedding: " + (data.embedding_ok ? "ok" : "fallback на keyword"));
      $("srch-result-meta").textContent = meta.join(" · ");
      const items = data.items || [];
      if (!items.length) {
        $("srch-items").innerHTML = '<p class="muted">Ничего не найдено. Возможно, индекс ещё не построен — зайдите на <a href="/dashboard">дашборд</a> и нажмите «Построить индекс».</p>';
      } else {
        $("srch-items").innerHTML = items.map(renderItem).join("");
      }
      $("srch-result").style.display = "block";
    } catch (e) {
      UI.showBanner("srch-error", e.message);
    } finally {
      $("srch-loading").style.display = "none";
      $("srch-run").disabled = false;
    }
  });

  $("srch-query").addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      $("srch-run").click();
    }
  });
})();
