(function () {
  var chart;
  var currentPage = 1;

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function buildPayload(page) {
    var product = document.getElementById("product").value.trim();
    var sourceRaw = document.getElementById("source").value.trim();
    var dateFrom = document.getElementById("date_from").value;
    var dateTo = document.getElementById("date_to").value;
    var payload = { page: page, page_size: 20 };
    if (product) payload.product_name = product;
    if (sourceRaw) payload.source_id = Number(sourceRaw);
    if (dateFrom) payload.date_from = new Date(dateFrom).toISOString();
    if (dateTo) payload.date_to = new Date(dateTo).toISOString();
    return payload;
  }

  function setLoading(loading) {
    var btn = document.getElementById("load");
    var label = document.getElementById("load-label");
    btn.disabled = loading;
    label.textContent = loading ? "Загрузка…" : "Загрузить";
  }

  function renderTable(items) {
    var tbody = document.getElementById("rows");
    var empty = document.getElementById("empty-table");
    tbody.innerHTML = "";
    if (!items || !items.length) {
      empty.style.display = "block";
      return;
    }
    empty.style.display = "none";
    items.forEach(function (i) {
      var tr = document.createElement("tr");
      var conf = typeof i.confidence === "number" ? i.confidence.toFixed(3) : esc(i.confidence);
      var src = i.source_id != null ? String(i.source_id) : "—";
      var sum = esc(i.summary || "");
      tr.innerHTML =
        "<td>" +
        esc(i.run_id) +
        "</td><td>" +
        esc(i.product_name || "") +
        "</td><td class=\"cell-muted\">" +
        src +
        "</td><td>" +
        conf +
        "</td><td class=\"cell-muted\">" +
        (window.MultiagentUI ? MultiagentUI.formatDateShort(i.created_at) : esc(i.created_at)) +
        "</td><td>" +
        sum +
        "</td>";
      tbody.appendChild(tr);
    });
  }

  function updateChart(kpi) {
    var pos = (kpi && kpi.positive_ratio) || 0;
    var neg = (kpi && kpi.negative_ratio) || 0;
    var neu = Math.max(0, 1 - pos - neg);
    var ctx = document.getElementById("sentimentChart");
    if (!ctx) return;
    if (chart) chart.destroy();
    chart = new Chart(ctx, {
      type: "doughnut",
      data: {
        labels: ["Позитивные (≥4★)", "Негативные (≤2★)", "Нейтральные"],
        datasets: [
          {
            data: [(pos * 100).toFixed(1), (neg * 100).toFixed(1), (neu * 100).toFixed(1)],
            backgroundColor: ["#3ee8b5", "#ff6b8a", "#ffc857"],
            borderWidth: 0,
            hoverOffset: 6,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        plugins: {
          legend: {
            position: "bottom",
            labels: { color: "#8b9dc4", padding: 16, font: { family: "'Plus Jakarta Sans', sans-serif" } },
          },
        },
      },
    });
  }

  async function loadDashboard() {
    window.MultiagentUI.hideBanner("dash-error");
    setLoading(true);
    try {
      var payload = buildPayload(currentPage);
      var res = await fetch("/insights/dashboard", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        var errText = await res.text();
        throw new Error(errText || res.statusText);
      }
      var data = await res.json();
      var kpi = data.kpi || {};

      document.getElementById("review_count").textContent =
        kpi.review_count != null ? String(Math.round(kpi.review_count)) : "—";
      document.getElementById("avg_rating").textContent =
        kpi.avg_rating != null ? Number(kpi.avg_rating).toFixed(2) : "—";
      document.getElementById("avg_confidence").textContent =
        data.avg_confidence != null ? Number(data.avg_confidence).toFixed(3) : "—";
      document.getElementById("total_runs").textContent =
        data.total_runs != null ? String(data.total_runs) : "—";

      var nr = (kpi.negative_ratio || 0) * 100;
      var pr = (kpi.positive_ratio || 0) * 100;
      document.getElementById("negative_ratio_big").textContent = nr.toFixed(1) + "%";
      document.getElementById("positive_ratio_big").textContent = pr.toFixed(1) + "%";

      renderTable(data.items);
      updateChart(kpi);

      currentPage = data.page || 1;
      var totalPages = data.total_pages || 1;
      document.getElementById("page-info").textContent =
        "Страница " + currentPage + " из " + totalPages + " · записей: " + (data.items && data.items.length);
      document.getElementById("prev-page").disabled = currentPage <= 1;
      document.getElementById("next-page").disabled = currentPage >= totalPages;
    } catch (e) {
      window.MultiagentUI.showBanner("dash-error", "Ошибка загрузки: " + (e.message || String(e)));
    } finally {
      setLoading(false);
    }
  }

  document.getElementById("load").onclick = function () {
    currentPage = 1;
    loadDashboard();
  };

  document.getElementById("prev-page").onclick = function () {
    if (currentPage > 1) {
      currentPage--;
      loadDashboard();
    }
  };

  document.getElementById("next-page").onclick = function () {
    currentPage++;
    loadDashboard();
  };

  document.getElementById("reset").onclick = function () {
    document.getElementById("date_from").value = "";
    document.getElementById("date_to").value = "";
  };

  document.getElementById("export").onclick = async function () {
    window.MultiagentUI.hideBanner("dash-error");
    try {
      var payload = buildPayload(1);
      payload.page = 1;
      payload.page_size = 200;
      var res = await fetch("/insights/dashboard/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(await res.text());
      var t = await res.text();
      var blob = new Blob([t], { type: "text/csv;charset=utf-8" });
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "dashboard-export.csv";
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      window.MultiagentUI.showBanner("dash-error", "Экспорт не удался: " + (e.message || String(e)));
    }
  };

  loadDashboard();
})();
