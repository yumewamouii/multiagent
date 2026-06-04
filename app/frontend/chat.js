/**
 * Простой чат поверх /chat/query.
 * - session_id сохраняется в localStorage, чтобы multi-turn пережил перезагрузку страницы.
 * - Кнопка «Очистить контекст» зовёт DELETE /chat/session/{id}.
 */
(function () {
  "use strict";

  const STORAGE_KEY_SESSION = "docdoc_chat_session_id";
  const STORAGE_KEY_HISTORY = "docdoc_chat_history_v1";
  const STORAGE_KEY_CITY = "docdoc_chat_city_slug";

  const messagesEl = document.getElementById("chat-messages");
  const formEl = document.getElementById("chat-form");
  const inputEl = document.getElementById("chat-input");
  const sendBtn = document.getElementById("chat-send");
  const resetBtn = document.getElementById("chat-reset");
  const sessionLabelEl = document.getElementById("chat-session-label");
  const routeLabelEl = document.getElementById("chat-route-label");
  const errorEl = document.getElementById("chat-error");
  const cityEl = document.getElementById("chat-city");
  const systemEl = document.getElementById("chat-system");
  const debugCardEl = document.getElementById("chat-debug-card");
  const debugEl = document.getElementById("chat-debug");

  const savedCity = localStorage.getItem(STORAGE_KEY_CITY) || "";
  if (savedCity) cityEl.value = savedCity;
  cityEl.addEventListener("change", () => {
    if (cityEl.value.trim()) {
      localStorage.setItem(STORAGE_KEY_CITY, cityEl.value.trim());
    } else {
      localStorage.removeItem(STORAGE_KEY_CITY);
    }
  });

  function getSessionId() {
    return localStorage.getItem(STORAGE_KEY_SESSION) || null;
  }
  function setSessionId(id) {
    if (id) localStorage.setItem(STORAGE_KEY_SESSION, id);
    else localStorage.removeItem(STORAGE_KEY_SESSION);
    renderSessionLabel();
  }

  function renderSessionLabel() {
    const id = getSessionId();
    sessionLabelEl.textContent = id ? "Сессия: " + id.slice(0, 8) + "…" : "Новая сессия";
  }

  function loadCachedHistory() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY_HISTORY);
      if (!raw) return [];
      return JSON.parse(raw) || [];
    } catch (_) {
      return [];
    }
  }
  function saveCachedHistory(messages) {
    try {
      const trimmed = messages.slice(-30);
      localStorage.setItem(STORAGE_KEY_HISTORY, JSON.stringify(trimmed));
    } catch (_) {
      /* localStorage might be full */
    }
  }

  let chatMessages = loadCachedHistory();

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function formatBotMarkdown(text) {
    // Минимальный markdown: **bold** и переносы строк.
    const safe = escapeHtml(text);
    return safe.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  }

  function renderMessages() {
    if (!chatMessages.length) {
      messagesEl.innerHTML =
        '<div class="chat-empty muted">Спросите что-нибудь, чтобы начать. Например, ' +
        '<em>«Проанализируй клинику Союз»</em>.</div>';
      return;
    }
    const parts = chatMessages.map(function (m) {
      const cls = m.role === "user" ? "chat-msg chat-msg-user" : "chat-msg chat-msg-bot";
      const content =
        m.role === "user" ? escapeHtml(m.content) : formatBotMarkdown(m.content || "");
      let meta = "";
      if (m.role === "bot" && (m.intent || m.route)) {
        const chips = [];
        if (m.route) {
          chips.push('<span class="chip" data-route="' + escapeHtml(m.route) + '">' + escapeHtml(m.route) + "</span>");
        }
        if (m.intent) {
          chips.push('<span class="chip">' + escapeHtml(m.intent) + "</span>");
        }
        if (typeof m.confidence === "number") {
          chips.push('<span class="muted">conf ' + m.confidence.toFixed(2) + "</span>");
        }
        if (chips.length) {
          meta = '<div class="chat-msg-meta">' + chips.join("") + "</div>";
        }
      }
      return '<div class="' + cls + '">' + content + meta + "</div>";
    });
    messagesEl.innerHTML = parts.join("");
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function pushMessage(msg) {
    chatMessages.push(msg);
    saveCachedHistory(chatMessages);
    renderMessages();
  }

  function showError(msg) {
    if (!msg) {
      errorEl.classList.remove("is-visible");
      errorEl.textContent = "";
      return;
    }
    errorEl.textContent = msg;
    errorEl.classList.add("is-visible");
  }

  function setRouteChip(route) {
    if (!route) {
      routeLabelEl.style.display = "none";
      return;
    }
    routeLabelEl.style.display = "inline-flex";
    routeLabelEl.textContent = route;
    routeLabelEl.dataset.route = route;
  }

  async function sendMessage(text) {
    const sessionId = getSessionId();
    const cityRaw = cityEl.value.trim();
    const sysOverride = systemEl.value || null;

    sendBtn.disabled = true;
    sendBtn.textContent = "…";
    showError("");

    pushMessage({ role: "user", content: text, ts: new Date().toISOString() });

    try {
      const resp = await fetch("/chat/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query: text,
          session_id: sessionId,
          city_slug: cityRaw || null,
          system_override: sysOverride,
        }),
      });
      if (!resp.ok) {
        const detail = await resp.text().catch(function () { return ""; });
        throw new Error("HTTP " + resp.status + ": " + (detail || "request_failed"));
      }
      const data = await resp.json();

      if (data.session && data.session.session_id) {
        setSessionId(data.session.session_id);
      }
      const route = (data.top_route && data.top_route.system) || null;
      setRouteChip(route);

      const intent = data.docdoc && data.docdoc.intent && data.docdoc.intent.intent;
      const confidence = data.docdoc && data.docdoc.intent && data.docdoc.intent.confidence;

      pushMessage({
        role: "bot",
        content: data.answer || "(пусто)",
        ts: new Date().toISOString(),
        intent: intent || null,
        confidence: typeof confidence === "number" ? confidence : null,
        route: route,
      });

      debugCardEl.hidden = false;
      debugEl.textContent = JSON.stringify(data, null, 2);
    } catch (err) {
      showError(err && err.message ? err.message : "Не удалось отправить запрос");
      pushMessage({
        role: "bot",
        content: "Произошла ошибка: " + (err && err.message ? err.message : "unknown"),
        ts: new Date().toISOString(),
        route: null,
      });
    } finally {
      sendBtn.disabled = false;
      sendBtn.textContent = "Отправить";
      inputEl.focus();
    }
  }

  formEl.addEventListener("submit", function (e) {
    e.preventDefault();
    const text = (inputEl.value || "").trim();
    if (!text) return;
    inputEl.value = "";
    sendMessage(text);
  });

  inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      formEl.requestSubmit();
    }
  });

  resetBtn.addEventListener("click", async function () {
    const sessionId = getSessionId();
    if (sessionId) {
      try {
        await fetch("/chat/session/" + encodeURIComponent(sessionId), { method: "DELETE" });
      } catch (_) {
        /* offline-safe */
      }
    }
    setSessionId(null);
    chatMessages = [];
    saveCachedHistory(chatMessages);
    setRouteChip(null);
    showError("");
    debugCardEl.hidden = true;
    renderMessages();
  });

  renderSessionLabel();
  renderMessages();
})();
