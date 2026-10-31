import type { Backup, Operation, OperationState, OperationsState, Release } from "./data";
import { formatBackupDate } from "./date";

const paths: Record<string, string> = {
  arrow: "M5 12h14m-5-5 5 5-5 5",
  refresh: "M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-1l2 2M4 16l2 2a7 7 0 0 0 12-1",
  plus: "M12 5v14M5 12h14",
  check: "m5 12 4 4L19 6",
  clock: "M12 8v4l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0",
  database:
    "M20 6c0 2-4 3-8 3S4 8 4 6s4-3 8-3 8 1 8 3ZM4 6v12c0 2 4 3 8 3s8-1 8-3V6M4 12c0 2 4 3 8 3s8-1 8-3",
  nodes: "M7 7h10M7 7l5 10M17 7l-5 10M7 4v6M4 7h6m7-3v6m-3-3h6m-8 7v6m-3-3h6",
  window: "M3 5h18v14H3ZM3 9h18M7 7h.01M10 7h.01",
  activity: "M3 12h4l3-8 4 16 3-8h4",
  globe: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0M3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18",
  external: "M14 3h7v7m0-7L10 14M10 3H4v17h17v-6",
  download: "M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5",
  shield: "m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Zm-4 9 3 3 5-6",
  close: "m6 6 12 12M6 18 18 6",
  sun: "M12 3v2m0 14v2M3 12h2m14 0h2M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0",
  moon: "M20 14A8 8 0 0 1 10 4a8 8 0 1 0 10 10Z",
  alert: "m12 3 10 18H2Zm0 6v5m0 3h.01",
  folder: "M3 7h7l2 2h9v11H3ZM3 7V4h7l2 3",
  play: "m8 4 12 8-12 8Z",
  search: "M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0Zm-2 5 6 6",
  settings: "M4 7h16M4 17h16M8 4v6m8 4v6",
};

export function icon(name: string, className = ""): string {
  return `<svg class="ops-icon ${className}" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] ?? paths.window}" /></svg>`;
}

export function escape(value: string): string {
  return value.replace(
    /[&<>"']/g,
    (character) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character] ??
      character,
  );
}

export function button(label: string, action: string, id = "", style = "", symbol = ""): string {
  const accessibleLabel = label || (action === "close-dialog" ? "Закрыть" : `Удалить копию ${id}`);
  return `<button type="button" class="ops-button ${style}" data-action="${action}" data-id="${escape(id)}" aria-label="${escape(accessibleLabel)}">${symbol ? icon(symbol) : ""}${label}</button>`;
}

export function badge(label: string, tone = "good", dot = true): string {
  return `<span class="ops-badge ops-badge--${tone}">${dot ? '<span class="ops-dot"></span>' : ""}${label}</span>`;
}

export function stateBadge(state: OperationState): string {
  const labels = {
    done: "Завершено",
    failed: "Ошибка",
    queued: "В очереди",
    running: "Выполняется",
    cancelled: "Отменено",
  };
  return badge(
    labels[state],
    { done: "good", failed: "bad", queued: "muted", running: "accent", cancelled: "muted" }[state],
  );
}

function heading(eyebrow: string, title: string, actions: string): string {
  return `<div class="ops-page-head"><div><div class="ops-eyebrow">${eyebrow}</div><h1 tabindex="-1">${title}</h1></div><div class="ops-actions">${actions}</div></div>`;
}

function metric(label: string, value: string, detail: string, symbol: string, extra = ""): string {
  return `<article class="ops-metric"><div class="ops-metric-label">${label}${icon(symbol)}</div><div class="ops-metric-value">${value}</div><div class="ops-metric-detail">${detail}</div>${extra}</article>`;
}

export function operationRows(state: OperationsState, limit?: number): string {
  const operations = state.operations.filter(
    (operation) =>
      (state.filter === "all" || operation.state === state.filter) &&
      `${operation.title} ${operation.id} ${escape(operation.kind)}`
        .toLowerCase()
        .includes(state.search.toLowerCase()),
  );
  return (
    (limit ? operations.slice(0, limit) : operations)
      .map(
        (operation) =>
          `<tr><td><button class="ops-row-link" data-action="operation" data-id="${operation.id}">${escape(operation.title)}</button><div class="ops-cell-sub"><span class="ops-mono">${operation.id}</span> · ${escape(operation.kind)}</div></td><td>${stateBadge(operation.state)}${operation.state === "running" ? `<div class="ops-inline-progress"><i style="width:${Math.max(5, (operation.step / Math.max(1, operation.stages.length)) * 100)}%"></i></div>` : ""}</td><td class="ops-muted ops-nowrap">${escape(operation.started)}</td><td class="ops-muted ops-nowrap">${escape(operation.duration)}</td><td class="ops-align-right">${button("Логи", "operation", operation.id, "ops-button--text", "arrow")}</td></tr>`,
      )
      .join("") ||
    '<tr><td colspan="5"><div class="ops-empty">Операций не найдено<span>Попробуйте изменить поиск или фильтр.</span></div></td></tr>'
  );
}

export function operationTable(state: OperationsState, limit?: number): string {
  return `<div class="ops-table-wrap"><table class="ops-table"><thead><tr><th>Операция</th><th>Статус</th><th>Запуск</th><th>Длительность</th><th></th></tr></thead><tbody class="ops-operation-list">${operationRows(state, limit)}</tbody></table></div>`;
}

function backupTime(backup: Backup): string {
  const { label, title } = formatBackupDate(backup.created);
  return `<time data-backup-time datetime="${escape(backup.created)}" title="${escape(title)}">${escape(label)}</time>`;
}

function latestBackup(backups: Backup[]): Backup | undefined {
  return backups.reduce<Backup | undefined>(
    (latest, backup) =>
      !latest || Date.parse(backup.created) > Date.parse(latest.created) ? backup : latest,
    undefined,
  );
}

export function renderStatus(state: OperationsState): string {
  const latest = latestBackup(state.backups);
  const healthy = state.services.filter((service) => service.healthy).length;
  const checks = state.checks.filter((check) => check.healthy).length;
  const warnings = state.checks.filter((check) => check.warning);
  return `${heading("ОБЗОР", "Состояние системы", button("Обновить", "refresh", "", "", "refresh"))}
    <div class="ops-metrics">
      ${metric("Сервисы", `${healthy} <span class="ops-value-suffix">/ ${state.services.length}</span>`, badge(state.services.length && healthy === state.services.length ? "Все работают" : state.services.length ? "Требуют внимания" : "Нет данных", state.services.length && healthy === state.services.length ? "good" : "warning"), "activity")}
      <article class="ops-metric ops-storage"><div class="ops-metric-label">Хранилища${icon("database")}</div>${state.storage.map((disk) => `<div class="ops-storage-item"><div class="ops-storage-line"><span>${escape(disk.name)}</span><strong>${escape(disk.used)} / ${escape(disk.total)}</strong></div><div class="ops-storage-usage">${disk.percent === null ? "Нет данных" : `Занято ${disk.percent}%`}</div><div class="ops-meter"><i style="width:${disk.percent ?? 0}%"></i></div></div>`).join("") || '<span class="ops-muted">Нет данных</span>'}</article>
      ${metric("Последний дамп", `<span class="ops-value-date">${latest ? backupTime(latest) : "Пока нет"}</span>`, latest ? `${badge(latest.verified ? "Целостность проверена" : "Не проверен", latest.verified ? "good" : "muted")}<span>${escape(latest.size)}</span>` : "Копии ещё не созданы", "shield", `<div class="ops-cell-sub">${escape(latest?.components.join(" · ") ?? "MongoDB · Neo4j · system · manifest")}</div>`)}
      ${metric("Основной сайт", state.live ? `Релиз <span class="ops-value-suffix">#${escape(state.live.id)}</span>` : "Нет данных о релизе", state.live ? `<span class="ops-mono">${escape(state.live.commit)}</span><span>Порт 8520</span>` : "Порт 8520", "globe")}
    </div>
    <div class="ops-overview-grid"><section class="ops-section"><div class="ops-section-head"><h2>Сервисы</h2>${badge(`${healthy} доступны`, "muted", false)}</div>
      <div class="ops-table-wrap"><table class="ops-table ops-services"><thead><tr><th>Сервис</th><th>Состояние</th><th>Порты</th><th>CPU</th><th>Память</th></tr></thead><tbody>${state.services.map((service) => `<tr><td><div class="ops-service-name"><span class="ops-service-icon">${icon(service.icon)}</span><div><strong>${escape(service.name)}</strong><div class="ops-cell-sub">${escape(service.role)}</div></div></div></td><td>${badge(escape(service.status), service.healthy ? "good" : "warning")}<div class="ops-cell-sub">${escape(service.version)}</div></td><td class="ops-mono ops-muted ops-nowrap">${escape(service.ports)}</td><td class="ops-mono ops-muted">${escape(service.cpu)}</td><td class="ops-mono ops-muted ops-nowrap">${escape(service.memory)}</td></tr>`).join("")}</tbody></table></div>
    </section><div class="ops-overview-aside"><section class="ops-section"><div class="ops-section-head"><h2>Проверки доступности</h2><span class="ops-muted">${checks} / ${state.checks.length}</span></div><div class="ops-checks">${state.checks.map((check) => `<div><span>${escape(check.label)}</span>${check.healthy ? icon("check", "ops-good") : badge(escape(check.detail), "warning", false)}</div>`).join("")}</div>${warnings.map((check) => `<div class="ops-notice">${icon("alert")}<span>${escape(check.warning ?? "")}</span></div>`).join("")}</section></div></div>`;
}

export function renderBackups(state: OperationsState): string {
  const latest = latestBackup(state.backups);
  return `${heading("ДАННЫЕ", "Резервные копии", button("Создать дамп", "create-backup", "", "ops-button--primary", "plus"))}
    <div class="ops-backup-summary"><div>${icon("shield")}<span><strong>${state.backups.length} копии сохранены</strong><span>Последняя — ${latest ? backupTime(latest) : "пока нет"}</span></span></div><div><span class="ops-muted">Хранилище</span><span class="ops-mono">essdata / pauk / dumps / ${escape(state.stand)}</span></div><div><span class="ops-muted">Состав копий</span><span>Состав указан у каждой копии</span></div></div>
    <section class="ops-section"><div class="ops-section-head"><h2>Сохранённые дампы</h2><span class="ops-muted">${state.backups.length} копий</span></div><div class="ops-table-wrap"><table class="ops-table"><thead><tr><th>Копия</th><th>Дата</th><th>Размер</th><th>Проверки</th><th></th></tr></thead><tbody>${state.backups.map((backup) => `<tr><td><strong>${escape(backup.name)}</strong><div class="ops-cell-sub ops-mono">${backup.id}</div><div class="ops-cell-sub">${escape(backup.components.join(" · "))}</div></td><td class="ops-muted ops-nowrap">${backupTime(backup)}</td><td class="ops-mono ops-nowrap">${backup.size}</td><td><div class="ops-backup-check">${!backup.managed ? badge("Старый формат", "muted") + '<span class="ops-cell-sub">Дата изменения файлов</span>' : badge(backup.verified ? "Целостность" : "Не проверен", backup.verified ? "good" : "muted")}${backup.managed ? (backup.restored ? badge("Восстановление", "good") : '<span class="ops-cell-sub">Восстановление не проверялось</span>') : ""}</div></td><td class="ops-align-right"><div class="ops-actions ops-actions--end">${backup.managed ? button("Проверить", "verify-backup", backup.id, "ops-button--text") : ""}${backup.managed ? button("Восстановить", "restore-backup", backup.id, "ops-button--text") : ""}${backup.managed ? button("", "delete-backup", backup.id, "ops-button--icon ops-danger", "close") : '<span class="ops-muted">Ручное восстановление</span>'}</div></td></tr>`).join("") || '<tr><td colspan="5"><div class="ops-empty">Копий пока нет<span>Создайте первый дамп или включите расписание.</span></div></td></tr>'}</tbody></table></div></section>
    <section class="ops-section ops-below"><div class="ops-section-head"><h2>Автоматическое создание</h2><button class="ops-switch" role="switch" aria-label="Автоматические дампы" aria-checked="${state.schedule.enabled}" data-action="toggle-schedule"><span></span></button></div><div class="ops-settings-rows"><div><span>Расписание</span><strong>Каждый день, ${state.schedule.time} <span class="ops-muted">МСК</span></strong></div><div><span>Хранить автоматических копий</span><strong>${state.schedule.keep} копий</strong></div><div><span>Следующий запуск</span><strong>${state.schedule.enabled ? `Ежедневно, ${state.schedule.time} МСК` : "Расписание выключено"}</strong></div></div><div class="ops-section-foot"><span>Запуск после завершения задач. Neo4j, админка и воркер временно останавливаются.</span>${button("Настроить", "schedule", "", "ops-button--text", "settings")}</div></section>`;
}

function graphPreview(): string {
  const points = Array.from({ length: 30 }, (_, index) => ({
    x: 65 + ((index * 83 + index * index * 7) % 370),
    y: 18 + ((index * 47 + index * index * 3) % 114),
  }));
  return `<svg class="ops-graph-preview" viewBox="0 0 500 155" fill="none" aria-hidden="true">${points
    .map((point, index) => {
      const other = points[(index * 7 + 3) % points.length];
      return other
        ? `<path d="M${point.x} ${point.y} ${other.x} ${other.y}" stroke="var(--muted)" stroke-opacity=".22"/>`
        : "";
    })
    .join(
      "",
    )}${points.map((point, index) => `<circle cx="${point.x}" cy="${point.y}" r="${index % 6 === 0 ? 5 : 2.5}" fill="${["var(--accent)", "#699a85", "#8c91b3", "#c4aa78"][index % 4]}" opacity="${index % 3 ? ".65" : "1"}"/>`).join("")}</svg>`;
}

function releaseCard(release: Release | null, live: boolean, state: OperationsState): string {
  const build = state.operations.find(
    (operation) => operation.state === "running" && operation.kind === "Сборка",
  );
  return `<section class="ops-section ops-release ${live ? "ops-release--live" : ""}"><div class="ops-section-head"><h2>${live ? "Основной сайт" : "Превью"}<span class="ops-port">:${live ? 8520 : 8521}</span></h2>${badge(live ? "Опубликован" : build ? "Собирается" : release ? "Готово к просмотру" : "Свободно", live ? "good" : build ? "accent" : "muted")}</div><div class="ops-release-preview">${graphPreview()}<span>PAUK <span>/</span> Карта научных связей</span></div>${release ? `<div class="ops-release-body"><div class="ops-release-title"><strong>Релиз #${release.id}</strong><span class="ops-muted">${release.created}</span></div><div class="ops-settings-rows"><div><span>Код</span><span class="ops-mono">${escape(release.commit)}</span></div><div><span>Ветка</span><span class="ops-truncate">${escape(release.branch)}</span></div><div><span>Данные</span><span class="ops-mono">${escape(release.snapshot)}</span></div></div><div class="ops-release-actions">${button(live ? "Открыть сайт" : "Открыть превью", "open-release", release.id, "", "external")}${live ? "" : button("Опубликовать", "publish", release.id, "ops-button--primary", "arrow")}${live ? "" : button("Убрать", "discard-preview", "", "ops-button--text")}</div></div>` : `<div class="ops-empty"><strong>${build ? "Подготавливаем новый релиз" : "Место для следующей версии"}</strong><span>${build ? escape(build.stages[build.step] ?? "Завершение") : "Соберите сайт, проверьте его здесь и опубликуйте."}</span>${build ? button("Ход сборки", "operation", build.id, "ops-button--text", "arrow") : button("Собрать превью", "build-site", "", "", "plus")}</div>`}</section>`;
}

export function renderSite(state: OperationsState): string {
  return `${heading("ПУБЛИКАЦИЯ", "Сайт и релизы", button("Собрать превью", "build-site", "", "ops-button--primary", "plus"))}
    <div class="ops-two-columns">${releaseCard(state.live, true, state)}${releaseCard(state.preview, false, state)}</div>
    <section class="ops-section ops-below"><div class="ops-section-head"><h2>GitHub Pages</h2>${badge(state.publicRelease ? "Опубликован" : "Публичный сайт", state.publicRelease ? "good" : "muted")}</div><div class="ops-release-body"><div class="ops-settings-rows"><div><span>Адрес</span><a href="https://itmo-nss-team.github.io/PAUK/" target="_blank" rel="noopener noreferrer">itmo-nss-team.github.io/PAUK/</a></div>${state.publicRelease ? `<div><span>Публичный релиз</span><strong>${escape(state.publicRelease.source)}</strong></div><div><span>Код</span><span class="ops-mono">${escape(state.publicRelease.commit)}</span></div><div><span>Обновлён</span><span>${escape(state.publicRelease.created)}</span></div>` : ""}</div><div class="ops-release-actions">${button("Обновить GitHub Pages", "publish-pages", "", "ops-button--primary", "refresh")}</div></div></section>
    <section class="ops-section ops-below"><div class="ops-section-head"><h2>Архив релизов</h2><span class="ops-muted">Порты 8522–8529 · ${state.archive.filter((release) => release.port).length} из 8 заняты</span></div><div class="ops-table-wrap"><table class="ops-table"><thead><tr><th>Релиз</th><th>Версия кода</th><th>Собран</th><th>Доступность</th><th></th></tr></thead><tbody>${state.archive.map((release) => `<tr><td><strong>#${release.id}</strong><div class="ops-cell-sub ops-mono">${release.snapshot}</div></td><td><span class="ops-mono">${escape(release.commit)}</span><div class="ops-cell-sub">${escape(release.branch)}</div></td><td class="ops-muted ops-nowrap">${release.created}</td><td>${badge(release.port ? `Порт ${release.port}` : "В архиве", release.port ? "good" : "muted")}</td><td><div class="ops-actions ops-actions--end">${release.port ? button("Открыть", "open-release", release.id, "ops-button--text") : ""}${button(release.port ? "Остановить" : "Запустить", release.port ? "stop-archive" : "start-archive", release.id, "ops-button--text")}${button("Вернуть", "rollback", release.id, "ops-button--text", "refresh")}</div></td></tr>`).join("")}</tbody></table></div></section>
`;
}

export function renderOperations(state: OperationsState): string {
  const active = state.operations.filter(
    (operation) => operation.state === "running" || operation.state === "queued",
  ).length;
  return `${heading("ЛОГИ", "Операции", badge(active ? `${active} в работе` : "Нет активных операций", active ? "accent" : "muted"))}
    <section class="ops-section"><div class="ops-operation-toolbar"><div class="ops-search">${icon("search")}<input type="search" id="ops-search" placeholder="Найти по названию или номеру" aria-label="Поиск операций" value="${escape(state.search)}"></div><select id="ops-state-filter" aria-label="Статус операции">${[
      ["all", "Все статусы"],
      ["running", "Выполняются"],
      ["queued", "В очереди"],
      ["done", "Завершены"],
      ["failed", "С ошибкой"],
      ["cancelled", "Отменены"],
    ]
      .map(
        ([value, label]) =>
          `<option value="${value}"${state.filter === value ? " selected" : ""}>${label}</option>`,
      )
      .join(
        "",
      )}</select></div>${operationTable(state)}<div class="ops-section-foot"><span>Операций: ${state.operations.length}</span></div></section>`;
}

export function operationDetails(operation: Operation): string {
  return `<div class="ops-operation-meta">${stateBadge(operation.state)}<span class="ops-mono ops-muted">${operation.id}</span><span class="ops-muted">${escape(operation.actor)}</span></div><ol class="ops-stages">${operation.stages.map((stage, index) => `<li class="${index < operation.step ? "is-done" : index === operation.step && operation.state === "running" ? "is-current" : operation.state === "failed" && index === operation.step ? "is-failed" : ""}"><span>${index < operation.step ? icon("check") : index + 1}</span><div>${escape(stage)}${index === operation.step && operation.state === "running" ? "<small>Выполняется…</small>" : ""}</div></li>`).join("")}</ol><div class="ops-log-head"><h3>Логи</h3>${button("Скачать", "download-log", operation.id, "ops-button--text", "download")}</div><pre class="ops-log">${operation.logs.map((line, index) => `<span class="ops-log-number">${String(index + 1).padStart(2, "0")}</span> ${escape(line)}`).join("\n") || "Ожидание запуска…"}</pre>${operation.state === "failed" ? '<div class="ops-notice">Проверьте причину ошибки перед повторным запуском.</div>' : ""}`;
}

export function backupDescription(backup: Backup): string {
  return `<div class="ops-selection-card">${icon("database")}<div><strong>${escape(backup.name)}</strong><span>${escape(backup.id)} · ${backupTime(backup)} · ${escape(backup.size)}</span></div></div>`;
}

export { graphPreview };
