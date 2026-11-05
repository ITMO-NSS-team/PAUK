import logo from "../../assets/icons/pauk-frame-accent.png";
import { themeById } from "../../core/themes";
import { initialState, pages } from "./data";
import type { Operation, OperationsState, Page } from "./data";
import {
  button,
  escape,
  icon,
  operationDetails,
  renderBackups,
  renderOperations,
  renderSite,
  renderStatus,
} from "./view";

const unsupported = "(max-width: 1023px), (hover: none) and (pointer: coarse)";

/** Mounts the operations client on desktop devices. */
export function mountOperations(): void {
  const root = document.getElementById("ops-app");
  const dialog = document.getElementById("ops-dialog");
  const toast = document.getElementById("ops-toast");
  if (!root || !(dialog instanceof HTMLDialogElement) || !toast) return;
  const state = initialState();
  const media = matchMedia(unsupported);
  let loaded = false;
  let fetching = false;
  let activeOperation: string | null = null;
  let submit: ((form: FormData) => Promise<void>) | null = null;
  let toastTimer: number | undefined;
  let submitting = false;
  let timer: number | undefined;
  const savedTheme = localStorage.getItem("pauk-ops-theme");
  state.theme = savedTheme === "light" ? "light" : "dark";

  function theme(): void {
    themeById(state.theme).apply(document.documentElement);
    document.documentElement.dataset.opsTheme = state.theme;
  }
  theme();

  function notify(message: string): void {
    if (!toast) return;
    toast.textContent = message;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => {
      toast.hidden = true;
    }, 6000);
  }

  async function api<T>(path: string, method = "GET", body?: unknown): Promise<T> {
    const response = await fetch(`/api/ops/${path}`, {
      method,
      cache: "no-store",
      credentials: "omit",
      headers: { "Content-Type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({ detail: "Сервер OPS недоступен" }));
      throw new Error(
        typeof data.detail === "string" ? data.detail : "Проверьте параметры операции",
      );
    }
    return response.json() as Promise<T>;
  }

  function render(): void {
    if (!root) return;
    const focused = document.activeElement;
    const focusId =
      focused instanceof HTMLInputElement || focused instanceof HTMLSelectElement ? focused.id : "";
    const cursor = focused instanceof HTMLInputElement ? focused.selectionStart : null;
    const count = state.operations.filter((operation) =>
      ["running", "queued"].includes(operation.state),
    ).length;
    const content = loaded
      ? {
          status: renderStatus,
          backups: renderBackups,
          site: renderSite,
          operations: renderOperations,
        }[state.page](state)
      : '<div class="ops-empty">Подключение к OPS…</div>';
    root.innerHTML = `<header class="ops-header"><div class="ops-header-inner"><div class="ops-brand"><img src="${logo}" width="26" height="26" alt=""><strong>PAUK</strong><span class="ops-brand-divider"></span><span class="ops-brand-label">OPS</span></div><nav class="ops-navigation" aria-label="Разделы управления">${pages.map((page) => `<a href="#${page.id}" class="${state.page === page.id ? "is-active" : ""}" ${state.page === page.id ? 'aria-current="page"' : ""}>${page.title}${page.id === "operations" && count ? `<span class="ops-nav-count">${count}</span>` : ""}</a>`).join("")}</nav><div class="ops-header-tools"><button class="ops-button ops-button--icon" data-action="theme" aria-label="Включить ${state.theme === "dark" ? "светлую" : "тёмную"} тему">${icon(state.theme === "dark" ? "sun" : "moon")}</button></div></div></header><main class="ops-main">${state.error ? `<div class="ops-notice" role="alert">${icon("alert")}${escape(state.error)}</div>` : ""}${content}</main><footer class="ops-footer"><span>PAUK / OPS${state.host ? ` · ${escape(state.host)}` : ""}</span></footer>`;
    if (focusId) {
      const input = document.getElementById(focusId);
      input?.focus({ preventScroll: true });
      if (input instanceof HTMLInputElement && cursor !== null)
        input.setSelectionRange(cursor, cursor);
    }
    updateOperation();
  }

  async function refresh(): Promise<void> {
    if (fetching || media.matches || document.hidden) return;
    fetching = true;
    try {
      const data = await api<OperationsState>("state");
      const { page, theme, search, filter } = state;
      Object.assign(state, data, { page, theme, search, filter });
      loaded = true;
    } catch (error) {
      state.error = error instanceof Error ? error.message : "Сервер OPS недоступен";
    } finally {
      fetching = false;
      render();
    }
  }

  function close(): void {
    if (dialog instanceof HTMLDialogElement) dialog.close();
    activeOperation = null;
    submit = null;
  }

  function show(
    title: string,
    content: string,
    action = "",
    handler?: (form: FormData) => Promise<void>,
  ): void {
    if (!(dialog instanceof HTMLDialogElement)) return;
    activeOperation = null;
    submit = handler ?? null;
    dialog.innerHTML = `<form class="ops-dialog-card"><header class="ops-dialog-head"><div><div class="ops-eyebrow">PAUK / OPS</div><h2 id="ops-dialog-title">${escape(title)}</h2></div>${button("", "close-dialog", "", "ops-button--icon", "close")}</header><div class="ops-dialog-body">${content}</div><footer class="ops-dialog-footer">${button("Закрыть", "close-dialog")}${action ? `<button type="submit" class="ops-button ops-button--primary">${escape(action)}</button>` : ""}</footer></form>`;
    if (!dialog.open) dialog.showModal();
  }

  function updateOperation(): void {
    if (!activeOperation || !(dialog instanceof HTMLDialogElement) || !dialog.open) return;
    const operation = state.operations.find((item) => item.id === activeOperation);
    const body = dialog.querySelector(".ops-dialog-body");
    if (operation && body) body.innerHTML = operationDetails(operation);
  }

  function inspect(operation: Operation): void {
    show(operation.title, operationDetails(operation));
    activeOperation = operation.id;
  }

  async function command(action: string, args: Record<string, unknown> = {}): Promise<void> {
    const operation = await api<Operation>("commands", "POST", { action, ...args });
    close();
    state.operations.unshift(operation);
    inspect(operation);
    await refresh();
  }

  function confirm(action: string, id = ""): void {
    const dangerous = [
      "restore-backup",
      "delete-backup",
      "publish",
      "rollback",
      "publish-pages",
    ].includes(action);
    const phrase = action === "restore-backup" ? `ВОССТАНОВИТЬ ${state.stand}` : "ПОДТВЕРЖДАЮ";
    const titles: Record<string, string> = {
      "restore-backup": "Восстановить базы из выбранного дампа?",
      "delete-backup": "Удалить выбранный дамп?",
      publish: "Опубликовать превью?",
      rollback: "Вернуть выбранный релиз?",
      "publish-pages": "Обновить GitHub Pages?",
      "discard-preview": "Убрать превью?",
      "stop-archive": "Остановить архивный релиз?",
      "create-backup": "Создать полный дамп?",
    };
    const downtime = ["restore-backup", "create-backup"].includes(action);
    show(
      titles[action] ?? "Подтвердить операцию",
      `${id ? `<div class="ops-selection-card"><strong>${escape(id)}</strong></div>` : ""}${downtime ? '<div class="ops-notice">На время операции Neo4j, админка и воркер будут остановлены. Восстановление начинается со страховочного дампа.</div>' : ""}${dangerous ? `<label class="ops-field">Введите ${escape(phrase)}<input name="confirmation" required autocomplete="off"></label>` : ""}`,
      "Продолжить",
      async (form) => {
        const confirmation = String(form.get("confirmation") ?? "");
        if (dangerous && confirmation !== phrase) throw new Error(`Введите ${phrase}`);
        await command(action, { id, confirmation });
      },
    );
  }

  async function handle(action: string, id: string): Promise<void> {
    if (action === "close-dialog") {
      close();
      return;
    }
    if (action === "theme") {
      state.theme = state.theme === "dark" ? "light" : "dark";
      localStorage.setItem("pauk-ops-theme", state.theme);
      theme();
      render();
      return;
    }
    if (action === "refresh") {
      await refresh();
      return;
    }
    if (!loaded) return;
    const operation = state.operations.find((item) => item.id === id);
    if (action === "operation" && operation) {
      inspect(operation);
      return;
    }
    if (action === "download-log" && operation) {
      const url = URL.createObjectURL(
        new Blob([operation.logs.join("\n")], { type: "text/plain;charset=utf-8" }),
      );
      const link = document.createElement("a");
      link.href = url;
      link.download = `${operation.id}.log`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      return;
    }

    if (action === "create-backup") {
      confirm(action);
      return;
    }
    if (action === "verify-backup") {
      await command(action, { id });
      return;
    }
    if (action === "publish-pages" && !state.pagesEnabled) {
      notify("Скрипт GitHub Pages ещё не настроен на сервере");
      return;
    }
    if (
      [
        "restore-backup",
        "delete-backup",
        "rollback",
        "discard-preview",
        "stop-archive",
        "publish-pages",
      ].includes(action)
    ) {
      confirm(action, id);
      return;
    }
    if (action === "publish") {
      if (state.preview) confirm(action, state.preview.id);
      return;
    }
    if (action === "build-site") {
      show(
        "Собрать превью",
        '<p class="ops-muted">Используется установленная на сервере версия кода. Основной сайт продолжает работать.</p><label class="ops-checkbox-field"><input type="checkbox" name="fresh" checked><span>Создать свежий снимок графа</span></label>',
        "Начать сборку",
        async (form) => {
          await command(action, { fresh: form.has("fresh") });
        },
      );
      return;
    }
    if (action === "open-release") {
      const release = [state.live, state.preview, ...state.archive].find((item) => item?.id === id);
      const port = state.live?.id === id ? 8520 : state.preview?.id === id ? 8521 : release?.port;
      if (port) window.open(`http://${state.host}:${port}/`, "_blank", "noopener");
      return;
    }
    if (action === "start-archive") {
      const occupied = new Set(state.archive.map((item) => item.port));
      const ports = Array.from({ length: 8 }, (_, index) => 8522 + index).filter(
        (port) => !occupied.has(port),
      );
      if (!ports.length) {
        notify("Нет свободных архивных портов");
        return;
      }
      show(
        "Запустить архивный релиз",
        `<label class="ops-field">Порт<select name="port">${ports.map((port) => `<option>${port}</option>`).join("")}</select></label>`,
        "Запустить",
        async (form) => {
          await command(action, { id, port: Number(form.get("port")) });
        },
      );
      return;
    }
    if (action === "schedule" || action === "toggle-schedule") {
      show(
        "Автоматическое создание дампов",
        `<div class="ops-notice">Каждая копия требует временной остановки Neo4j, админки и воркера. Хранятся последние автоматические копии; ручные и страховочные сохраняются.</div><label class="ops-checkbox-field"><input type="checkbox" name="enabled" ${state.schedule.enabled ? "checked" : ""}><span>Включить расписание</span></label><label class="ops-field">Время по Москве<input name="time" type="time" value="${state.schedule.time}" required></label><label class="ops-field">Хранить автоматических копий<input name="keep" type="number" min="1" max="100" value="${state.schedule.keep}" required></label>`,
        "Сохранить",
        async (form) => {
          await api("schedule", "PUT", {
            enabled: form.has("enabled"),
            time: form.get("time"),
            keep: Number(form.get("keep")),
          });
          close();
          await refresh();
        },
      );
    }
  }

  function click(event: Event): void {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const node = target.closest<HTMLElement>("[data-action]");
    if (node?.dataset.action)
      void handle(node.dataset.action, node.dataset.id ?? "").catch((error: unknown) =>
        notify(error instanceof Error ? error.message : "Не удалось выполнить действие"),
      );
  }
  root.addEventListener("click", click);
  dialog.addEventListener("click", click);
  dialog.addEventListener("close", () => {
    if (!dialog.open) {
      activeOperation = null;
      submit = null;
    }
  });
  dialog.addEventListener("submit", (event) => {
    event.preventDefault();
    if (
      submitting ||
      !(event.target instanceof HTMLFormElement) ||
      !event.target.reportValidity() ||
      !submit
    )
      return;
    submitting = true;
    const buttons = dialog.querySelectorAll<HTMLButtonElement>('button[type="submit"]');
    buttons.forEach((node) => {
      node.disabled = true;
    });
    void submit(new FormData(event.target))
      .catch((error: unknown) =>
        notify(error instanceof Error ? error.message : "Операция не запущена"),
      )
      .finally(() => {
        submitting = false;
        buttons.forEach((node) => {
          node.disabled = false;
        });
      });
  });
  root.addEventListener("change", (event) => {
    if (event.target instanceof HTMLSelectElement && event.target.id === "ops-state-filter") {
      state.filter = event.target.value;
      render();
    }
  });
  root.addEventListener("input", (event) => {
    if (event.target instanceof HTMLInputElement && event.target.id === "ops-search") {
      state.search = event.target.value;
      render();
    }
  });
  function navigate(): void {
    const hash = location.hash.slice(1);
    state.page = pages.some((page) => page.id === hash) ? (hash as Page) : "status";
    close();
    render();
  }
  function device(): void {
    root?.toggleAttribute("inert", media.matches);
    if (media.matches) {
      close();
      clearInterval(timer);
      timer = undefined;
    } else {
      void refresh();
      if (timer === undefined)
        timer = window.setInterval(() => {
          void refresh();
        }, 15000);
    }
  }
  window.addEventListener("hashchange", navigate);
  media.addEventListener("change", device);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) void refresh();
  });
  window.addEventListener(
    "pagehide",
    () => {
      clearInterval(timer);
      clearTimeout(toastTimer);
    },
    { once: true },
  );
  navigate();
  device();
}
