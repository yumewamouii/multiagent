(function () {
  var outStructured = document.getElementById("out-structured");
  var outRaw = document.getElementById("out-raw");
  var outPlaceholder = document.getElementById("out-placeholder");
  var outLoading = document.getElementById("out-loading");

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function setLoading(on) {
    outLoading.style.display = on ? "block" : "none";
  }

  function clearError() {
    window.MultiagentUI.hideBanner("query-error");
  }

  function showError(msg) {
    window.MultiagentUI.showBanner("query-error", msg);
  }

  function hideOutputs() {
    outStructured.style.display = "none";
    outRaw.style.display = "none";
    outPlaceholder.style.display = "none";
  }

  function showRaw(obj) {
    hideOutputs();
    outRaw.style.display = "block";
    outRaw.textContent = JSON.stringify(obj, null, 2);
  }

  function sentimentClass(s) {
    var t = (s || "").toLowerCase();
    if (t.indexOf("neg") >= 0 || t.indexOf("bad") >= 0) return "badge-neg";
    if (t.indexOf("pos") >= 0 || t.indexOf("good") >= 0) return "badge-pos";
    return "badge-neu";
  }

  function renderMultiAgent(data) {
    hideOutputs();
    outStructured.style.display = "block";
    var ev = data.evidence || [];
    var evHtml = ev
      .map(function (e) {
        return (
          '<li class="citation-card"><div class="meta">#' +
          e.review_id +
          " · " +
          escapeHtml(e.product_name || "") +
          ' · <span class="badge ' +
          sentimentClass(e.sentiment) +
          '">' +
          (e.sentiment || "") +
          "</span></div><div>" +
          escapeHtml(e.summary || "") +
          "</div></li>"
        );
      })
      .join("");
    outStructured.innerHTML =
      '<p class="muted" style="margin-top:0">Маршрут: <strong>' +
      escapeHtml(data.route || "—") +
      "</strong> · уверенность: <strong>" +
      (data.confidence != null ? Number(data.confidence).toFixed(3) : "—") +
      "</strong></p>" +
      '<div class="prose" style="margin:16px 0">' +
      escapeHtml(data.answer || "").replace(/\n/g, "<br>") +
      "</div>" +
      (data.critic_notes
        ? '<p class="muted" style="font-size:0.9rem"><strong>Критик:</strong> ' +
          escapeHtml(data.critic_notes || "") +
          "</p>"
        : "") +
      (ev.length ? "<h3 style=\"font-size:1rem;margin:20px 0 10px\">Доказательная база</h3><ul class=\"citation-list\">" + evHtml + "</ul>" : "");
  }

  function renderRag(data) {
    hideOutputs();
    outStructured.style.display = "block";
    var cit = data.citations || [];
    var citHtml = cit
      .map(function (c) {
        return (
          '<li class="citation-card"><div class="meta">#' +
          c.rank +
          " · review " +
          c.review_id +
          " · " +
          escapeHtml(c.product_name || "") +
          ' · <span class="badge ' +
          sentimentClass(c.sentiment) +
          '">' +
          (c.sentiment || "") +
          "</span></div><div>" +
          escapeHtml(c.summary || "") +
          "</div></li>"
        );
      })
      .join("");
    var metrics = data.metrics || {};
    var mKeys = Object.keys(metrics);
    var metricsHtml =
      mKeys.length > 0
        ? "<p class=\"muted\" style=\"font-size:0.85rem\">Метрики: " +
          mKeys
            .map(function (k) {
              return k + "=" + metrics[k];
            })
            .join(", ") +
          "</p>"
        : "";
    outStructured.innerHTML =
      metricsHtml +
      '<div class="prose" style="margin:16px 0">' +
      escapeHtml(data.answer || "").replace(/\n/g, "<br>") +
      "</div>" +
      (cit.length ? "<h3 style=\"font-size:1rem;margin:20px 0 10px\">Цитаты</h3><ul class=\"citation-list\">" + citHtml + "</ul>" : "");
  }

  document.querySelectorAll(".tab-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var id = btn.getAttribute("data-tab");
      document.querySelectorAll(".tab-btn").forEach(function (b) {
        b.classList.toggle("is-active", b === btn);
        b.setAttribute("aria-selected", b === btn ? "true" : "false");
      });
      document.querySelectorAll(".tab-panel").forEach(function (p) {
        p.classList.toggle("is-active", p.id === "panel-" + id);
      });
    });
  });

  document.getElementById("sync").onclick = async function () {
    clearError();
    var query = document.getElementById("q").value.trim();
    if (query.length < 2) {
      showError("Введите не менее 2 символов.");
      return;
    }
    var top_k = Number(document.getElementById("topk").value || 5);
    setLoading(true);
    hideOutputs();
    outPlaceholder.style.display = "none";
    try {
      var res = await fetch("/multiagent/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: query, top_k: top_k }),
      });
      var data = await res.json();
      if (!res.ok) throw new Error(data.detail ? JSON.stringify(data.detail) : res.statusText);
      renderMultiAgent(data);
    } catch (e) {
      showError(e.message || String(e));
      outPlaceholder.style.display = "block";
    } finally {
      setLoading(false);
    }
  };

  document.getElementById("async").onclick = async function () {
    clearError();
    var query = document.getElementById("q").value.trim();
    if (query.length < 2) {
      showError("Введите не менее 2 символов.");
      return;
    }
    var top_k = Number(document.getElementById("topk").value || 5);
    setLoading(true);
    hideOutputs();
    outPlaceholder.style.display = "none";
    try {
      var res = await fetch("/multiagent/query/async", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: query, top_k: top_k }),
      });
      var job = await res.json();
      if (!res.ok) throw new Error(JSON.stringify(job));
      var done = false;
      while (!done) {
        await new Promise(function (r) {
          setTimeout(r, 1200);
        });
        var st = await fetch("/multiagent/jobs/" + job.job_id);
        var data = await st.json();
        if (!st.ok) throw new Error(JSON.stringify(data));
        done = data.status === "done" || data.status === "failed";
        if (data.status === "done" && data.result) renderMultiAgent(data.result);
        else if (data.status === "failed") {
          if (data.error) showError(data.error);
          showRaw(data);
        }
      }
    } catch (e) {
      showError(e.message || String(e));
      outPlaceholder.style.display = "block";
    } finally {
      setLoading(false);
    }
  };

  document.getElementById("rag-send").onclick = async function () {
    clearError();
    var query = document.getElementById("rq").value.trim();
    if (query.length < 2) {
      showError("Введите не менее 2 символов.");
      return;
    }
    var top_k = Number(document.getElementById("rtopk").value || 5);
    setLoading(true);
    hideOutputs();
    try {
      var res = await fetch("/rag/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: query, top_k: top_k }),
      });
      var data = await res.json();
      if (!res.ok) throw new Error(data.detail ? JSON.stringify(data.detail) : res.statusText);
      renderRag(data);
    } catch (e) {
      showError(e.message || String(e));
      outPlaceholder.style.display = "block";
    } finally {
      setLoading(false);
    }
  };
})();
