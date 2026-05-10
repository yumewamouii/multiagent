(function () {
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

  async function runSearch() {
    window.MultiagentUI.hideBanner("search-error");
    var q = document.getElementById("search-q").value.trim();
    var topk = Number(document.getElementById("search-topk").value || 8);
    var list = document.getElementById("search-results");
    var empty = document.getElementById("search-empty");
    var loading = document.getElementById("search-loading");

    if (q.length < 2) {
      window.MultiagentUI.showBanner("search-error", "Запрос должен содержать минимум 2 символа.");
      return;
    }

    loading.style.display = "block";
    list.innerHTML = "";
    empty.style.display = "none";

    try {
      var url = "/knowledge/search?query=" + encodeURIComponent(q) + "&top_k=" + topk;
      var res = await fetch(url);
      var data = await res.json();
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data));

      loading.style.display = "none";
      if (!data.length) {
        empty.style.display = "block";
        return;
      }

      data.forEach(function (item) {
        var li = document.createElement("li");
        li.className = "citation-card";
        li.innerHTML =
          '<div class="meta">Отзыв #' +
          item.review_id +
          " · " +
          escapeHtml(item.product_name || "") +
          ' · <span class="badge ' +
          sentimentClass(item.sentiment) +
          '">' +
          escapeHtml(item.sentiment || "") +
          "</span>" +
          (item.tags ? " · " + escapeHtml(item.tags || "") : "") +
          "</div>" +
          '<div style="margin-bottom:8px;font-weight:500">' +
          escapeHtml(item.summary || "") +
          "</div>" +
          (item.review_text
            ? '<div class="muted" style="font-size:0.88rem;line-height:1.5">' +
              escapeHtml(item.review_text || "").slice(0, 800) +
              (item.review_text.length > 800 ? "…" : "") +
              "</div>"
            : "");
        list.appendChild(li);
      });
    } catch (e) {
      loading.style.display = "none";
      window.MultiagentUI.showBanner("search-error", "Ошибка: " + (e.message || String(e)));
    }
  }

  document.getElementById("search-btn").onclick = runSearch;
  document.getElementById("search-q").addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") runSearch();
  });
})();
