(function () {
  "use strict";
  const UI = window.MultiagentUI;
  const $ = (id) => document.getElementById(id);
  const esc = UI.escapeHtml;

  function fmtNum(v, suffix) {
    if (v === null || v === undefined) return "—";
    return v + (suffix || "");
  }

  function ratingClass(v) {
    if (v === null || v === undefined) return "";
    if (v >= 8.5) return "is-good";
    if (v >= 6) return "is-warn";
    return "is-bad";
  }
  function negClass(v) {
    if (v === null || v === undefined) return "";
    if (v >= 30) return "is-bad";
    if (v >= 15) return "is-warn";
    return "is-good";
  }

  function renderKpis(metrics, responseStatus) {
    const m = metrics || {};
    const rs = responseStatus || {};
    const cards = [
      { label: "Отзывов", value: fmtNum(m.reviews_count) },
      { label: "Средняя оценка", value: fmtNum(m.avg_rating), cls: ratingClass(m.avg_rating) },
      { label: "Медиана", value: fmtNum(m.median_rating), cls: ratingClass(m.median_rating) },
      { label: "Доля негатива", value: fmtNum(m.negative_share_pct, "%"), cls: negClass(m.negative_share_pct) },
      { label: "Без ответа", value: fmtNum(m.unanswered_share_pct, "%"), cls: negClass(m.unanswered_share_pct) },
      { label: "Риск (без ответа, низ.)", value: fmtNum(m.negative_unanswered_count) },
      { label: "Доля ответов", value: fmtNum(rs.answered_share_pct, "%") },
    ];
    return cards.map(function (c) {
      return (
        '<div class="kpi-card"><div class="kpi-label">' + esc(c.label) + "</div>" +
        '<div class="kpi-value ' + (c.cls || "") + '">' + esc(c.value) + "</div></div>"
      );
    }).join("");
  }

  function renderList(items, emptyMsg) {
    if (!items || !items.length) {
      return '<li class="muted">' + esc(emptyMsg || "Нет данных для этого блока") + "</li>";
    }
    return items.map(function (s) { return "<li>" + esc(s) + "</li>"; }).join("");
  }

  function renderRiskReviews(items) {
    if (!items || !items.length) return '<p class="muted" style="margin:0">Нет.</p>';
    return items.map(function (r) {
      const head = "Оценка " + fmtNum(r.rating) + ", " + (r.answered ? "есть ответ" : "без ответа");
      const meta = [];
      if (r.clinic_name) meta.push("клиника: " + esc(r.clinic_name));
      if (r.service_name) meta.push("услуга: " + esc(r.service_name));
      if (r.doctor_name) meta.push("врач: " + esc(r.doctor_name));
      if (r.review_id) meta.push("review_id=" + r.review_id);
      return (
        '<div class="snippet">' +
        '<div class="snippet-title">' + esc(head) + "</div>" +
        "<div>" + esc(r.text || "") + "</div>" +
        '<div class="snippet-meta">' + meta.map(function (m) { return '<span class="tag">' + m + "</span>"; }).join("") + "</div>" +
        "</div>"
      );
    }).join("");
  }

  function renderReplies(items) {
    if (!items || !items.length) return "";
    return items.map(function (rep) {
      const head = "Ответ к review_id=" + (rep.review_id || "?") + (rep.tone ? ", тон: " + esc(rep.tone) : "");
      const points = (rep.talking_points || []).map(function (p) { return "<li>" + esc(p) + "</li>"; }).join("");
      return (
        '<div class="snippet">' +
        '<div class="snippet-title">' + esc(head) + "</div>" +
        "<div>" + esc(rep.draft_reply || "") + "</div>" +
        (points ? '<ul class="bullet-list" style="margin-top:6px">' + points + "</ul>" : "") +
        "</div>"
      );
    }).join("");
  }

  // ---------- suggestions ----------

  async function refreshSuggestions() {
    const t = $("rep-type").value;
    const meta = $("rep-sugg-meta");
    meta.textContent = "(загружаем…)";
    const { items, source } = await UI.loadEntitiesWithFallback(t, { limit: 30 });
    UI.fillDatalist("rep-entity-options", items);
    UI.bindSuggestions({
      mountId: "rep-suggestions",
      inputId: "rep-entity",
      items: items.slice(0, 12),
    });
    if (!items.length) {
      meta.textContent = "(нет данных — запустите краул)";
    } else {
      meta.textContent = "(" + items.length + (items.length >= 30 ? "+" : "") + ", " + (source === "json-fallback" ? "из JSON" : "из БД") + ")";
    }
  }

  $("rep-type").addEventListener("change", refreshSuggestions);
  window.addEventListener("data-source-changed", refreshSuggestions);
  UI.whenReady(refreshSuggestions);

  // ---------- preset buttons (только подсветка интента) ----------

  Array.from(document.querySelectorAll(".chip-btn[data-preset]")).forEach(function (b) {
    b.addEventListener("click", function () {
      const p = b.dataset.preset;
      $("rep-gen-replies").checked = p === "risk";
      $("rep-use-rag").checked = true;
      $("rep-use-llm").checked = true;
    });
  });

  // ---------- run ----------

  $("rep-run").addEventListener("click", async () => {
    UI.showBanner("rep-error", "");
    UI.showBanner("rep-info", "");
    const entity = $("rep-entity").value.trim();
    if (!entity) {
      UI.showBanner("rep-error", "Укажите имя сущности (entity).");
      return;
    }
    const ds = UI.DataSource.get();
    const body = {
      entity_type: $("rep-type").value,
      entity: entity,
      city_slug: ($("rep-city").value.trim() || ds.city_slug) || null,
      data_source: $("rep-data-source").value || ds.data_source,
      crawl_path: $("rep-crawl-path").value.trim() || ds.crawl_path || null,
      rag_top_k: Number($("rep-rag-top-k").value),
      reviews_in_prompt: Number($("rep-reviews-in-prompt").value),
      risk_reviews_count: Number($("rep-risk-count").value),
      use_rag: $("rep-use-rag").checked,
      use_llm: $("rep-use-llm").checked,
      generate_reply_drafts: $("rep-gen-replies").checked,
    };

    $("rep-loading").style.display = "block";
    $("rep-result").style.display = "none";
    $("rep-run").disabled = true;

    try {
      const data = await UI.api("POST", "/docdoc/reputation/analyze", body);
      if (!data.ok) {
        UI.showBanner("rep-error", "Ошибка: " + (data.error || "unknown") + (data.hint ? " — " + data.hint : ""));
        return;
      }
      $("rep-title").textContent = "Отчёт: " + (data.entity_name || data.entity_id || "");
      $("rep-summary").innerHTML = renderKpis(data.metrics, data.response_status);

      const r = data.report || {};
      if (data.report_source === "heuristic") {
        UI.showBanner(
          "rep-info",
          "Текстовые блоки ниже собраны автоматически из отзывов (LLM не вернул JSON"
            + (data.llm_error ? ": " + data.llm_error : "")
            + "). Для более глубокого анализа запустите LM Studio и включите use_llm."
        );
      } else if (data.report_source === "llm") {
        UI.showBanner("rep-info", "");
      } else if (!(data.metrics && data.metrics.reviews_count)) {
        UI.showBanner("rep-info", "По этой сущности не найдено отзывов в выбранном источнике данных.");
      }

      $("rep-exec").innerHTML = r.executive_summary
        ? '<h3>Executive summary</h3><p>' + esc(r.executive_summary) + "</p>"
        : '<p class="muted">Краткая сводка не сформирована.</p>';
      $("rep-praises").innerHTML = renderList(r.what_patients_value, "Нет явных формулировок в положительных отзывах");
      $("rep-complaints").innerHTML = renderList(r.top_complaints, "Нет явных жалоб в негативных отзывах");
      $("rep-improvements").innerHTML = renderList(r.service_improvements, "Нет рекомендаций");
      $("rep-landing").innerHTML = renderList(r.landing_page_gaps, "Нет рекомендаций для страницы");
      $("rep-ad").textContent = r.ad_angle || "Нет данных — мало отзывов или LLM недоступен.";
      $("rep-audience").textContent = r.target_audience || "Нет данных.";
      $("rep-risk-topics").innerHTML = renderList(r.risk_topics, "Риск-темы не выделены");
      $("rep-risk-reviews").innerHTML = renderRiskReviews(data.risk_reviews || []);
      const replies = data.reply_drafts || [];
      if (replies.length) {
        $("rep-replies").innerHTML = renderReplies(replies);
        $("rep-replies-block").style.display = "block";
      } else {
        $("rep-replies-block").style.display = "none";
      }
      $("rep-raw").textContent = JSON.stringify(data, null, 2);
      $("rep-result").style.display = "block";
    } catch (e) {
      UI.showBanner("rep-error", e.message);
    } finally {
      $("rep-loading").style.display = "none";
      $("rep-run").disabled = false;
    }
  });
})();
