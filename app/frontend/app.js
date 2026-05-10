(function () {
  const path = window.location.pathname;

  document.querySelectorAll("[data-nav]").forEach(function (link) {
    const href = link.getAttribute("href");
    if (!href) return;
    if (href === path || (path === "/" && link.dataset.nav === "home")) {
      link.classList.add("is-active");
    }
  });

  const toggle = document.querySelector("[data-menu-toggle]");
  const panel = document.querySelector("[data-menu-panel]");
  if (toggle && panel) {
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

  window.MultiagentUI = {
    showBanner: function (id, message) {
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = message;
      el.classList.add("is-visible");
    },
    hideBanner: function (id) {
      const el = document.getElementById(id);
      if (!el) return;
      el.classList.remove("is-visible");
      el.textContent = "";
    },
    formatDateShort: function (iso) {
      if (!iso) return "—";
      try {
        const d = new Date(iso);
        return d.toLocaleString("ru-RU", {
          day: "2-digit",
          month: "short",
          year: "numeric",
          hour: "2-digit",
          minute: "2-digit",
        });
      } catch {
        return iso;
      }
    },
  };
})();
