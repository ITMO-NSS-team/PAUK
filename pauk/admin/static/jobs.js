/* Poll only the job lists; forms and filters outside them stay untouched. */
(function () {
  "use strict";
  const ACTIVE_MS = 15000;
  const IDLE_MS = 60000;
  const MAX_BACKOFF_MS = 120000;
  const REQUEST_TIMEOUT_MS = 10000;
  const GROUPS_INTERVAL_MS = 120000;

  function createJobsPoller(env) {
    const doc = env.document;
    let timer = null;
    let inFlight = false;
    let stopped = false;
    let resume = false;
    let failures = 0;
    const initialMarker = doc.getElementById("jobs-fragment");
    let active = initialMarker.dataset.active === "true";
    let groupsActive = new Set((initialMarker.dataset.groupsActive || "").split(",").filter(Boolean));
    let groupsPending = false;
    let groupsLastSuccess = env.Date ? env.Date.now() : Date.now();
    let groupsRetryAfter = 0;

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

    function syncPage(page) {
      const params = new URLSearchParams(env.location.search);
      if (params.has("page") && params.get("page") !== page) {
        params.set("page", page);
        env.history.replaceState(env.history.state, "", `${env.location.pathname}?${params}${env.location.hash}`);
      }
    }

    function replaceLists(parsed) {
      const current = [doc.getElementById("jobs-active"), doc.getElementById("jobs-history"),
        doc.getElementById("jobs-graph-banner")];
      const incoming = [parsed.getElementById("jobs-active"), parsed.getElementById("jobs-history"),
        parsed.getElementById("jobs-graph-banner")];
      if (incoming.some((item) => !item)) throw new Error("Invalid jobs fragment");
      const historyFocused = current[1].contains(doc.activeElement);
      if (!historyFocused) {
        const opened = new Set([...current[1].querySelectorAll("details[data-job-details][open]")]
          .map((item) => item.dataset.jobDetails));
        incoming[1].querySelectorAll("details[data-job-details]").forEach((item) => {
          item.open = opened.has(item.dataset.jobDetails);
        });
      }
      current.forEach((item, index) => {
        if (!item.contains(doc.activeElement)) item.replaceChildren(...incoming[index].childNodes);
      });
      return !historyFocused;
    }

    async function refreshGroups(marker) {
      const container = doc.getElementById("jobs-groups");
      if (!container) return;
      const current = new Set((marker.dataset.groupsActive || "").split(",").filter(Boolean));
      if ([...groupsActive].some((id) => !current.has(id))) groupsPending = true;
      groupsActive = current;
      const now = env.Date.now();
      if ((!groupsPending && now - groupsLastSuccess < GROUPS_INTERVAL_MS) ||
          now < groupsRetryAfter || container.contains(doc.activeElement)) return;
      const controller = new env.AbortController();
      const timeout = env.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
      try {
        const response = await env.fetch("/jobs/groups", {
          headers: { Accept: "application/json" }, cache: "no-store", credentials: "same-origin",
          signal: controller.signal,
        });
        if (response.status === 401 || response.status === 403 || response.redirected) {
          stopped = true;
          status("\u0421\u0435\u0441\u0441\u0438\u044f \u0437\u0430\u0432\u0435\u0440\u0448\u0438\u043b\u0430\u0441\u044c. ", true);
          return;
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const parsed = new env.DOMParser().parseFromString(await response.text(), "text/html");
        const incoming = parsed.getElementById("jobs-groups");
        if (!incoming || !incoming.querySelector("#group")) throw new Error("Invalid groups fragment");
        if (doc.hidden || container.contains(doc.activeElement)) {
          groupsPending = true;
          return;
        }
        const previous = container.querySelector("#group");
        const selected = previous.value;
        const needsChoice = !selected && [...previous.options].some((option) => option.value === "" && option.disabled);
        container.replaceChildren(...incoming.childNodes);
        const select = container.querySelector("#group");
        if (selected && [...select.options].some((option) => option.value === selected)) {
          select.value = selected;
        } else if (selected || needsChoice) {
          const option = doc.createElement("option");
          option.value = "";
          option.disabled = true;
          option.textContent = "\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0433\u0440\u0443\u043f\u043f\u0443";
          select.prepend(option);
          select.value = "";
        }
        groupsLastSuccess = env.Date.now();
        groupsRetryAfter = 0;
        groupsPending = false;
      } catch (error) {
        groupsPending = true;
        groupsRetryAfter = env.Date.now() + GROUPS_INTERVAL_MS;
      } finally {
        env.clearTimeout(timeout);
      }
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
        if (!marker || !["true", "false"].includes(marker.dataset.active) ||
            !/^[1-9]\d*$/.test(marker.dataset.page)) throw new Error("Invalid response");
        if (doc.hidden) return;
        if (replaceLists(parsed)) syncPage(marker.dataset.page);
        active = marker.dataset.active === "true";
        failures = 0;
        status("", false);
        await refreshGroups(marker);
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
    syncPage(doc.getElementById("jobs-fragment").dataset.page);
    schedule(active ? ACTIVE_MS : IDLE_MS);
    return { poll };
  }

  if (typeof module !== "undefined" && module.exports) module.exports = { createJobsPoller };
  if (typeof window !== "undefined" && document.getElementById("jobs-fragment")) createJobsPoller(window);
}());
