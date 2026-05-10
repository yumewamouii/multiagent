(function () {
  var barChart;

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function sentimentClass(s) {
    var t = (s || "").toLowerCase();
    if (t.indexOf("neg") >= 0) return "badge-neg";
    if (t.indexOf("pos") >= 0) return "badge-pos";
    return "badge-neu";
  }

  function buildPayload() {
    var name = document.getElementById("p-name").value.trim();
    var sourceRaw = document.getElementById("p-source").value.trim();
    var topk = Number(document.getElementById("p-topk").value || 8);
    var from = document.getElementById("p-from").value;
    var to = document.getElementById("p-to").value;
    var payload = { product_name: name, top_k: topk };
    if (sourceRaw) payload.source_id = Number(sourceRaw);
    if (from) payload.date_from = new Date(from).toISOString();
    if (to) payload.date_to = new Date(to).toISOString();
    return payload;
  }

  function updateSentimentChart(breakdown) {
    var ctx = document.getElementById("sentimentBar");
    if (!ctx || typeof Chart === "undefined") return;
    var entries = Object.entries(breakdown || {});
    if (!entries.length) {
      if (barChart) barChart.destroy();
      return;
    }
    var labels = entries.map(function (e) {
      return e[0];
    });
    var values = entries.map(function (e) {
      return e[1];
    });
    var colors = labels.map(function (l) {
      var t = l.toLowerCase();
      if (t.indexOf("neg") >= 0) return "#ff6b8a";
      if (t.indexOf("pos") >= 0) return "#3ee8b5";
      return "#ffc857";
    });
    if (barChart) barChart.destroy();
    barChart = new Chart(ctx, {
      type: "bar",
      data: {
        labels: labels,
        datasets: [
          {
            data: values,
            backgroundColor: colors,
            borderRadius: 8,
          },
        ],
      },
      options: {
        responsive: true,
        plugins: {
          legend: { display: false },
        },
        scales: {
          x: {
            ticks: { color: "#8b9dc4" },
            grid: { color: "rgba(120,140,220,0.1)" },
          },
          y: {
            ticks: { color: "#8b9dc4" },
            grid: { color: "rgba(120,140,220,0.1)" },
            beginAtZero: true,
          },
        },
      },
    });
  }

  document.getElementById("p-run").onclick = async function () {
    window.MultiagentUI.hideBanner("product-error");
    var name = document.getElementById("p-name").value.trim();
    if (name.length < 2) {
      window.MultiagentUI.showBanner("product-error", "Укажите название товара (от 2 символов).");
      return;
    }

    var btn = document.getElementById("p-run");
    var label = document.getElementById("p-run-label");
    btn.disabled = true;
    label.textContent = "Считаем…";

    try {
      var res = await fetch("/insights/product", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildPayload()),
      });
      var data = await res.json();
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data));

      document.getElementById("product-results").style.display = "block";
      document.getElementById("p-summary").innerHTML =
        "<p>" + escapeHtml(data.summary || "").replace(/\n/g, "<br>") + "</p>";
      document.getElementById("p-route").textContent = data.route || "—";
      document.getElementById("p-rag").innerHTML =
        "<p>" + escapeHtml(data.rag_answer || "").replace(/\n/g, "<br>") + "</p>";

      var tags = document.getElementById("p-tags");
      tags.innerHTML = "";
      (data.top_tags || []).forEach(function (t) {
        var span = document.createElement("span");
        span.className = "tag";
        span.textContent = t;
        tags.appendChild(span);
      });

      document.getElementById("p-critic").textContent = JSON.stringify(data.critic || {}, null, 2);

      var citList = document.getElementById("p-citations");
      citList.innerHTML = "";
      (data.citations || []).forEach(function (c) {
        var li = document.createElement("li");
        li.className = "citation-card";
        li.innerHTML =
          '<div class="meta">#' +
          c.rank +
          " · " +
          escapeHtml(c.product_name || "") +
          ' · <span class="badge ' +
          sentimentClass(c.sentiment) +
          '">' +
          escapeHtml(c.sentiment || "") +
          "</span></div><div>" +
          escapeHtml(c.summary || "") +
          "</div>";
        citList.appendChild(li);
      });

      var mcp = document.getElementById("p-mcp");
      mcp.innerHTML = "";
      (data.mcp_flow || []).forEach(function (m) {
        var div = document.createElement("div");
        div.className = "timeline-item";
        div.innerHTML =
          '<div class="muted" style="font-size:0.78rem">' +
          (m.created_at || "") +
          "</div><strong>" +
          (m.from_agent || "") +
          " → " +
          (m.to_agent || "") +
          "</strong> · " +
          (m.intent || "") +
          '<pre style="margin:8px 0 0;font-size:0.8rem;white-space:pre-wrap" class="muted">' +
          JSON.stringify(m.payload || {}, null, 2) +
          "</pre>";
        mcp.appendChild(div);
      });

      updateSentimentChart(data.sentiment_breakdown || {});
    } catch (e) {
      window.MultiagentUI.showBanner("product-error", e.message || String(e));
    } finally {
      btn.disabled = false;
      label.textContent = "Сформировать инсайт";
    }
  };
})();
