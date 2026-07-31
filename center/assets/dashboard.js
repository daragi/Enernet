(() => {
  const dateElements = document.querySelectorAll("[data-current-date]");
  const timeElements = document.querySelectorAll("[data-current-time]");
  const yearElements = document.querySelectorAll("[data-current-year]");
  const addressElements = document.querySelectorAll("[data-server-address]");
  const statusCards = document.querySelectorAll("[data-server-status], .sidebar-status");

  const formatters = {
    date: new Intl.DateTimeFormat("ko-KR", {
      year: "numeric",
      month: "long",
      day: "numeric",
      weekday: "short",
    }),
    time: new Intl.DateTimeFormat("ko-KR", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }),
  };

  const updateClock = () => {
    const now = new Date();
    dateElements.forEach((element) => {
      element.textContent = formatters.date.format(now);
    });
    timeElements.forEach((element) => {
      element.textContent = formatters.time.format(now);
    });
    yearElements.forEach((element) => {
      element.textContent = String(now.getFullYear());
    });
  };

  const setServerState = (state) => {
    const labels = {
      online: "서버 연결됨",
      offline: "서버 연결 안 됨",
      checking: "서버 확인 중",
    };

    statusCards.forEach((card) => {
      card.classList.toggle("is-online", state === "online");
      card.classList.toggle("is-offline", state === "offline");
      card.classList.toggle("is-checking", state === "checking");
      const text = card.querySelector("[data-server-status-text]") || card.querySelector("strong");
      if (text) text.textContent = labels[state];
      card.title = state === "online" ? "서버가 정상 응답 중입니다." : state === "offline" ? "서버 응답을 확인할 수 없습니다." : "서버 상태를 확인하고 있습니다.";
    });
  };

  const checkServer = async () => {
    if (!statusCards.length) return;
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), 5000);

    try {
      const response = await fetch("/api/health", {
        cache: "no-store",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (payload.status && !["ok", "healthy", "ready"].includes(String(payload.status).toLowerCase())) {
        throw new Error("unhealthy");
      }
      setServerState("online");
    } catch (_error) {
      setServerState("offline");
    } finally {
      window.clearTimeout(timeoutId);
    }
  };

  if (window.location.host) {
    addressElements.forEach((element) => {
      element.textContent = window.location.host;
    });
  }

  updateClock();
  setServerState("checking");
  checkServer();
  window.setInterval(updateClock, 1000);
  window.setInterval(checkServer, 15000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) checkServer();
  });
})();
