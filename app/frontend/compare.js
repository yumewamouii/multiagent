(function () {
  "use strict";
  const UI = window.MultiagentUI;
  const $ = (id) => document.getElementById(id);
  const esc = UI.escapeHtml;

  // ---------- entity rows ----------

  function renderEntityRow(idx, mode) {
    const showType = mode === "mixed";
    return (
      '<div class="entity-row" data-idx="' + idx + '">' +
      (showType
        ? '<select class="entity-type" style="max-width:140px">' +
          '<option value="clinic">clinic</option>' +
          '<option value="service">service</option>' +
          '<option value="category">category</option>' +
          '<option value="doctor">doctor</option>' +
          "</select>"
        : "") +
      '<input type="text" class="entity-value" list="cmp-options" placeholder="имя или подстрока">' +
      '<button type="button" class="btn btn-ghost entity-remove" title="Удалить">×</button>' +
      "</div>"
    );
  }

  function rebuildEntities() {
    const wrap = $("cmp-entities");
    const mode = $("cmp-mode").value;
    const existing = Array.from(wrap.querySelectorAll(".entity-row")).map(function (row) {
      const t = row.querySelector(".entity-type");
      const v = row.querySelector(".entity-value");
      return { type: t ? t.value : null, value: v ? v.value : "" };
    });
    while (existing.length < 2) existing.push({ type: "clinic", value: "" });
    wrap.innerHTML = existing.slice(0, 6).map(function (_, i) { return renderEntityRow(i, mode); }).join("");
    Array.from(wrap.querySelectorAll(".entity-row")).forEach(function (row, i) {
      const v = row.querySelector(".entity-value");
      if (v) v.value = existing[i] ? existing[i].value : "";
      const t = row.querySelector(".entity-type");
      if (t && existing[i] && existing[i].type) t.value = existing[i].type;
    });
    attachEntityListeners();
    if (mode === "mixed") {
      Array.from(wrap.querySelectorAll(".entity-type")).forEach(function (sel) {
        sel.addEventListener("change", refreshSuggestions);
      });
    }
  }

  function attachEntityListeners() {
    Array.from(document.querySelectorAll(".entity-remove")).forEach(function (btn) {
      btn.onclick = function () {
        const wrap = $("cmp-entities");
        if (wrap.querySelectorAll(".entity-row").length <= 2) return;
        btn.closest(".entity-row").remove();
      };
    });
  }

  function fillFirstEmpty(name) {
    const inputs = Array.from(document.querySelectorAll(".entity-row .entity-value"));
    for (const inp of inputs) {
      if (!inp.value.trim()) {
        inp.value = name;
        return;
      }
    }
    if (inputs.length < 6) {
      $("cmp-add").click();
      const last = document.querySelectorAll(".entity-row .entity-value");
      if (last.length) last[last.length - 1].value = name;
    }
  }

  $("cmp-mode").addEventListener("change", function () {
    const mode = $("cmp-mode").value;
    $("cmp-common-type-field").style.display = mode === "mixed" ? "none" : "";
    rebuildEntities();
    refreshSuggestions();
  });

  $("cmp-common-type").addEventListener("change", refreshSuggestions);

  $("cmp-add").addEventListener("click", function () {
    const wrap = $("cmp-entities");
    if (wrap.querySelectorAll(".entity-row").length >= 6) return;
    const idx = wrap.querySelectorAll(".entity-row").length;
    const tmp = document.createElement("div");
    tmp.innerHTML = renderEntityRow(idx, $("cmp-mode").value);
    wrap.appendChild(tmp.firstChild);
    attachEntityListeners();
  });

  rebuildEntities();

  // ---------- suggestions ----------

  async function refreshSuggestions() {
    const meta = $("cmp-sugg-meta");
    meta.textContent = "(загружаем…)";
    const t = $("cmp-mode").value === "mixed" ? "clinic" : $("cmp-common-type").value;
    const { items, source } = await UI.loadEntitiesWithFallback(t, { limit: 30 });
    UI.fillDatalist("cmp-options", items);
    UI.bindSuggestions({
      mountId: "cmp-suggestions",
      items: items.slice(0, 12),
      onClick: fillFirstEmpty,
    });
    if (!items.length) {
      meta.textContent = "(нет данных — запустите краул)";
    } else {
      meta.textContent = "(" + items.length + (items.length >= 30 ? "+" : "") + ", " + (source === "json-fallback" ? "из JSON" : "из БД") + ")";
    }
  }

  async function refreshScopeOptions() {
    const tasks = [
      ["service", "scope-service-options"],
      ["category", "scope-category-options"],
      ["clinic", "scope-clinic-options"],
      ["doctor", "scope-doctor-options"],
    ];
    for (const [type, listId] of tasks) {
      const { items } = await UI.loadEntitiesWithFallback(type, { limit: 30 });
      UI.fillDatalist(listId, items);
    }
  }

  UI.whenReady(function () {
    refreshSuggestions();
    refreshScopeOptions();
  });
  window.addEventListener("data-source-changed", function () {
    refreshSuggestions();
    refreshScopeOptions();
  });

  // ---------- run ----------

  function collectEntities(mode) {
    const rows = Array.from(document.querySelectorAll(".entity-row"));
    return rows.map(function (row) {
      const value = row.querySelector(".entity-value").value.trim();
      if (!value) return null;
      if (mode === "mixed") {
        const type = row.querySelector(".entity-type").value;
        return { type: type, value: value };
      }
      return value;
    }).filter(function (x) { return !!x; });
  }

  function collectScope() {
    const out = {};
    ["service", "category", "clinic", "doctor"].forEach(function (k) {
      const v = $("scope-" + k).value.trim();
      if (v) out[k] = v;
    });
    return Object.keys(out).length ? out : null;
  }

  function renderMetricsTable(items) {
    if (!items || !items.length) return '<p class="muted">Нет данных.</p>';
    const rows = items.map(function (it) {
      const m = it.metrics || {};
      const rs = it.response_status || {};
      return (
        "<tr>" +
        "<td>" + esc(it.entity_name || it.entity_id || "—") + "</td>" +
        "<td>" + esc(it.entity_type || "—") + "</td>" +
        '<td class="num">' + esc(m.reviews_count != null ? String(m.reviews_count) : "—") + "</td>" +
        '<td class="num">' + esc(m.avg_rating != null ? String(m.avg_rating) : "—") + "</td>" +
        '<td class="num">' + esc(m.negative_share_pct != null ? m.negative_share_pct + "%" : "—") + "</td>" +
        '<td class="num">' + esc(rs.answered_share_pct != null ? rs.answered_share_pct + "%" : "—") + "</td>" +
        '<td class="num">' + esc(it.rag_snippets_count != null ? String(it.rag_snippets_count) : "—") + "</td>" +
        "</tr>"
      );
    }).join("");
    return (
      '<table class="data-table">' +
      "<thead><tr>" +
      "<th>Сущность</th><th>Тип</th>" +
      '<th class="num">отзывы</th>' +
      '<th class="num">avg</th>' +
      '<th class="num">негатив</th>' +
      '<th class="num">ответы</th>' +
      '<th class="num">RAG чанков</th>' +
      "</tr></thead>" +
      "<tbody>" + rows + "</tbody></table>"
    );
  }

  function renderWinners(winners) {
    if (!winners) return "";
    const labels = { avg_rating: "Лучший рейтинг", answer_rate: "Лучше отвечают", review_volume: "Больше отзывов" };
    const cards = Object.keys(labels).map(function (k) {
      const v = winners[k];
      return (
        '<div class="kpi-card">' +
        '<div class="kpi-label">' + esc(labels[k]) + "</div>" +
        '<div class="kpi-value">' + esc(v || "—") + "</div></div>"
      );
    });
    return '<h3>Победители по метрикам</h3><div class="summary-grid">' + cards.join("") + "</div>";
  }

  function renderPerEntity(items) {
    if (!items || !items.length) return '<p class="muted">—</p>';
    return items.map(function (it) {
      const strengths = (it.strengths || []).map(function (s) { return "<li>" + esc(s) + "</li>"; }).join("");
      const weaknesses = (it.weaknesses || []).map(function (s) { return "<li>" + esc(s) + "</li>"; }).join("");
      const usp = (it.unique_selling_points || []).map(function (s) { return "<li>" + esc(s) + "</li>"; }).join("");
      return (
        '<div class="compare-card">' +
        "<h4>" + esc(it.entity_name || it.entity_id || "—") + "</h4>" +
        '<div class="report-section"><strong>Сильные</strong><ul class="bullet-list">' + (strengths || '<li class="muted">—</li>') + "</ul></div>" +
        '<div class="report-section"><strong>Слабые</strong><ul class="bullet-list">' + (weaknesses || '<li class="muted">—</li>') + "</ul></div>" +
        (usp ? '<div class="report-section"><strong>USP</strong><ul class="bullet-list">' + usp + "</ul></div>" : "") +
        "</div>"
      );
    }).join("");
  }

  function renderShared(arr) {
    if (!arr || !arr.length) return '<li class="muted">—</li>';
    return arr.map(function (s) { return "<li>" + esc(s) + "</li>"; }).join("");
  }

  function renderWarnings(data) {
    const parts = [];
    if (data.scope) {
      const tags = Object.keys(data.scope).map(function (k) { return '<span class="tag">' + esc(k) + ": " + esc(data.scope[k]) + "</span>"; }).join(" ");
      parts.push('<p>Применённый scope: ' + tags + "</p>");
    }
    if (data.scope_empty && data.scope_empty.length) {
      parts.push('<p class="muted">У этих сущностей не осталось отзывов после scope: ' +
        data.scope_empty.map(function (s) { return '<span class="tag">' + esc(s.type || "?") + ": " + esc(s.value) + "</span>"; }).join(" ") +
        "</p>");
    }
    if (data.not_found && data.not_found.length) {
      parts.push('<p class="muted">Не нашлись: ' +
        data.not_found.map(function (s) {
          if (typeof s === "string") return '<span class="tag">' + esc(s) + "</span>";
          return '<span class="tag">' + esc(s.type || "?") + ": " + esc(s.value) + "</span>";
        }).join(" ") + "</p>");
    }
    return parts.join("");
  }

  $("cmp-run").addEventListener("click", async () => {
    UI.showBanner("cmp-error", "");
    const mode = $("cmp-mode").value;
    const entities = collectEntities(mode);
    if (entities.length < 2) {
      UI.showBanner("cmp-error", "Нужно минимум 2 сущности.");
      return;
    }
    const ds = UI.DataSource.get();
    const body = {
      entity_type: mode === "mixed" ? null : $("cmp-common-type").value,
      entities: entities,
      city_slug: ($("cmp-city").value.trim() || ds.city_slug) || null,
      data_source: $("cmp-data-source").value || ds.data_source,
      crawl_path: $("cmp-crawl-path").value.trim() || ds.crawl_path || null,
      use_rag: $("cmp-use-rag").checked,
      use_llm: $("cmp-use-llm").checked,
      rag_top_k: Number($("cmp-rag-top-k").value),
      reviews_per_entity: Number($("cmp-reviews-per-entity").value),
      scope: collectScope(),
    };

    $("cmp-loading").style.display = "block";
    $("cmp-result").style.display = "none";
    $("cmp-run").disabled = true;

    try {
      const data = await UI.api("POST", "/docdoc/reputation/compare", body);
      if (!data.ok) {
        UI.showBanner("cmp-error", "Ошибка: " + (data.error || "unknown") + (data.hint ? " — " + data.hint : ""));
        $("cmp-raw").textContent = JSON.stringify(data, null, 2);
        return;
      }
      const cmp = data.compare || {};
      if (data.compare_source === "heuristic") {
        UI.showBanner(
          "cmp-info",
          "Текстовые блоки ниже собраны автоматически из отзывов (LLM не вернул JSON"
            + (data.llm_error ? ": " + data.llm_error : "")
            + "). Для более глубокого анализа запустите LM Studio и включите use_llm."
        );
      } else {
        UI.showBanner("cmp-info", "");
      }
      $("cmp-summary").innerHTML = cmp.summary
        ? "<h3>Итог</h3><p>" + esc(cmp.summary) + "</p>"
        : "";
      $("cmp-winners").innerHTML = renderWinners(cmp.winner_by_metric);
      $("cmp-metrics-table-wrap").innerHTML = renderMetricsTable(data.items || []);
      $("cmp-per-entity").innerHTML = renderPerEntity(cmp.per_entity || []);
      $("cmp-shared").innerHTML = renderShared(cmp.shared_complaints || []);
      $("cmp-ad").textContent = cmp.ad_angle || "—";
      $("cmp-warnings").innerHTML = renderWarnings(data);
      $("cmp-raw").textContent = JSON.stringify(data, null, 2);
      $("cmp-result").style.display = "block";
    } catch (e) {
      UI.showBanner("cmp-error", e.message);
    } finally {
      $("cmp-loading").style.display = "none";
      $("cmp-run").disabled = false;
    }
  });
})();
