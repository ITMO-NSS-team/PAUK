const moscowDate = new Intl.DateTimeFormat("ru-RU", {
  timeZone: "Europe/Moscow",
  year: "numeric",
  month: "numeric",
  day: "numeric",
});

export function formatBackupDate(
  value: string,
  now = new Date(),
): { label: string; title: string } {
  const date = new Date(value);
  const parts = (input: Date) => {
    const values = Object.fromEntries(
      moscowDate.formatToParts(input).map(({ type, value }) => [type, value]),
    );
    return { year: Number(values.year), month: Number(values.month), day: Number(values.day) };
  };
  const day = parts(date);
  const today = parts(now);
  const dayNumber = (v: typeof day) => Date.UTC(v.year, v.month - 1, v.day) / 86_400_000;
  const difference = dayNumber(today) - dayNumber(day);
  const time = new Intl.DateTimeFormat("ru-RU", {
    timeZone: "Europe/Moscow",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(date);
  const dateLabel =
    difference === 0
      ? "Сегодня"
      : difference === 1
        ? "Вчера"
        : new Intl.DateTimeFormat("ru-RU", {
            timeZone: "Europe/Moscow",
            day: "numeric",
            month: "long",
            ...(day.year !== today.year ? { year: "numeric" } : {}),
          }).format(date);
  const title = new Intl.DateTimeFormat("ru-RU", {
    timeZone: "Europe/Moscow",
    day: "numeric",
    month: "long",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
    timeZoneName: "short",
  }).format(date);
  return { label: `${dateLabel}, ${time}`, title };
}
