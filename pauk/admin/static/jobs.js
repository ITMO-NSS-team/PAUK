/* Poll only the job lists; forms and filters outside them stay untouched. */
(function () {
  "use strict";
  const ACTIVE_MS = 15000;
  const IDLE_MS = 60000;
  const MAX_BACKOFF_MS = 120000;
  const REQUEST_TIMEOUT_MS = 10000;

  function createJobsPoller(env) {
    const doc = env.document;
    let timer = null;
    let inFlight = false;
    let stopped = false;
    let resume = false;
    let failures = 0;
    let active = doc.getElementById("jobs-fragment").dataset.active === "true";

    function schedule(delay) {
      if (timer !== null) env.clearTimeout(timer);
      timer = null;
      if (!stopped && !doc.hidden) timer = env.setTimeout(poll, delay);
    }

    function status(message, login) {
      const box = doc.getElementById("jobs-refresh-status");
      box.replaceChildren(doc.createTextNode(message));
      if (login) {
        const link = doc.createElement("a");
        link.href = "/login?next=%2Fjobs";
        link.textContent = "Войти снова";
        box.append(" ", link);
      }
      box.hidden = !message;
    }

    function replaceLists(parsed) {
      const current = [doc.getElementById("jobs-active"), doc.getElementById("jobs-history"),
        doc.getElementById("jobs-graph-banner")];
      const incoming = [parsed.getElementById("jobs-active"), parsed.getElementById("jobs-history"),
        parsed.getElementById("jobs-graph-banner")];
      if (incoming.some((item) => !item)) throw new Error("Invalid jobs fragment");
      if (current.some((item) => item.contains(doc.activeElement))) return;
      const opened = new Set([...current[1].querySelectorAll("details[data-job-details][open]")]
        .map((item) => item.dataset.jobDetails));
      incoming[1].querySelectorAll("details[data-job-details]").forEach((item) => {
        item.open = opened.has(item.dataset.jobDetails);
      });
      current.forEach((item, index) => item.replaceChildren(...incoming[index].childNodes));
    }

    async function poll() {
      if (timer !== null) env.clearTimeout(timer);
      timer = null;
      if (stopped || doc.hidden || inFlight) return;
      inFlight = true;
      const controller = new env.AbortController();
      const requestTimeout = env.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
      try {
        const params = new URLSearchParams(env.location.search);
        const query = new URLSearchParams();
        for (const key of ["kind", "state", "actor", "page"]) {
          if (params.has(key)) query.set(key, params.get(key));
        }
        const response = await env.fetch(`/jobs/fragment?${query}`, {
          headers: { Accept: "application/json" }, cache: "no-store", credentials: "same-origin",
          signal: controller.signal,
        });
        if (response.status === 401 || response.status === 403 || response.redirected) {
          stopped = true;
          status("Сессия завершилась. ", true);
          return;
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const parsed = new env.DOMParser().parseFromString(await response.text(), "text/html");
        const marker = parsed.getElementById("jobs-fragment");
        if (!marker || !["true", "false"].includes(marker.dataset.active)) throw new Error("Invalid response");
        if (doc.hidden) return;
        replaceLists(parsed);
        active = marker.dataset.active === "true";
        failures = 0;
        status("", false);
      } catch (error) {
        failures += 1;
        status("Не удалось обновить задания. Повторим попытку.", false);
      } finally {
        env.clearTimeout(requestTimeout);
        inFlight = false;
        if (resume && !doc.hidden && !stopped) {
          resume = false;
          schedule(0);
        } else {
          const base = active ? ACTIVE_MS : IDLE_MS;
          schedule(failures ? Math.min(base * (2 ** failures), MAX_BACKOFF_MS) : base);
        }
      }
    }

    doc.addEventListener("visibilitychange", () => {
      if (doc.hidden) {
        if (timer !== null) env.clearTimeout(timer);
        timer = null;
        resume = false;
      } else if (inFlight) {
        resume = true;
      } else {
        schedule(0);
      }
    });
    schedule(active ? ACTIVE_MS : IDLE_MS);
    return { poll };
  }

  if (typeof module !== "undefined" && module.exports) module.exports = { createJobsPoller };
  if (typeof window !== "undefined" && document.getElementById("jobs-fragment")) createJobsPoller(window);
}());
