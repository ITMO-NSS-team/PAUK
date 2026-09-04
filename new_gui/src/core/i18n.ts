// Полный i18n (t()/LOCALES для статичных строк интерфейса) переносится из
// старого i18n.js отдельным шагом. Здесь — только выбор языка в данных,
// которые генератор уже отдаёт билингвально парами "x"/"x_en".

export type Lang = "ru" | "en";

/**
 * Возвращает obj[`${base}_en`] для lang === "en" (если поле непустое),
 * иначе obj[base]. Пример: localize(check, "title", "en") вместо
 * ручного `lang === "en" ? check.title_en : check.title` в каждой вкладке.
 */
export function localize<T extends Record<string, unknown>>(
  obj: T,
  base: string,
  lang: Lang,
): string {
  const ruValue = obj[base];
  if (lang === "ru") return String(ruValue ?? "");

  const enValue = obj[`${base}_en`];
  return enValue ? String(enValue) : String(ruValue ?? "");
}
