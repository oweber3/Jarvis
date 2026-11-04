// Dates for the thread and the chat list. Everything uses the browser's own locale and local calendar days.

export type AgeGroup = "today" | "yesterday" | "week" | "month" | "older"

export const AGE_GROUP_ORDER: readonly { group: AgeGroup; label: string }[] = [
  { group: "today", label: "Today" },
  { group: "yesterday", label: "Yesterday" },
  { group: "week", label: "Previous 7 days" },
  { group: "month", label: "Previous 30 days" },
  { group: "older", label: "Older" },
]

const startOfDay = (date: Date) => new Date(date.getFullYear(), date.getMonth(), date.getDate())

// Building an Intl formatter costs far more than using one, and every message's time is formatted on each
// redraw, so each formatter is built once per locale and options.
const dateFormats = new Map<string, Intl.DateTimeFormat>()
const relativeFormats = new Map<string, Intl.RelativeTimeFormat>()

function dateFormat(locale: string | undefined, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${locale ?? ""}|${JSON.stringify(options)}`
  let format = dateFormats.get(key)
  if (!format) {
    format = new Intl.DateTimeFormat(locale, options)
    dateFormats.set(key, format)
  }
  return format
}

function relativeFormat(locale: string | undefined): Intl.RelativeTimeFormat {
  const key = locale ?? ""
  let format = relativeFormats.get(key)
  if (!format) {
    format = new Intl.RelativeTimeFormat(locale, { numeric: "auto" })
    relativeFormats.set(key, format)
  }
  return format
}

// Whole local calendar days from ``ts`` to ``now`` (negative for the future).
function daysBetween(ts: number, now: Date): number {
  const a = startOfDay(new Date(ts * 1000))
  const b = startOfDay(now)
  return Math.round((b.getTime() - a.getTime()) / 86_400_000)
}

/** Builds the formatters ahead of need (the first relative one loads the browser's locale data, which is slow), so a
 * page can do it while it waits for its first answer rather than while it draws a chat. */
export function prepareFormats(locale?: string): void {
  relativeFormat(locale)
  dateFormat(locale, { hour: "numeric", minute: "2-digit" })
  dateFormat(locale, { dateStyle: "full", timeStyle: "short" })
}

export function dayKey(ts: number): string {
  const date = new Date(ts * 1000)
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`
}

export function startsNewDay(previousTs: number | undefined, ts: number): boolean {
  return previousTs === undefined || dayKey(previousTs) !== dayKey(ts)
}

// "today" and "yesterday" come from the browser's own word for them; other days get a full date.
export function dayDividerLabel(ts: number, now: Date = new Date(), locale?: string): string {
  const days = daysBetween(ts, now)
  if (days === 0 || days === 1) {
    return relativeFormat(locale).format(-days, "day")
  }
  const sameYear = new Date(ts * 1000).getFullYear() === now.getFullYear()
  return dateFormat(locale, {
    weekday: "long",
    day: "numeric",
    month: "long",
    ...(sameYear ? {} : { year: "numeric" }),
  }).format(new Date(ts * 1000))
}

export function messageTime(ts: number, locale?: string): string {
  return dateFormat(locale, { hour: "numeric", minute: "2-digit" }).format(new Date(ts * 1000))
}

export function fullDateTime(ts: number, locale?: string): string {
  return dateFormat(locale, { dateStyle: "full", timeStyle: "short" }).format(new Date(ts * 1000))
}

export function ageGroup(ts: number, now: Date = new Date()): AgeGroup {
  const days = daysBetween(ts, now)
  if (days <= 0) return "today"
  if (days === 1) return "yesterday"
  if (days <= 7) return "week"
  if (days <= 30) return "month"
  return "older"
}

// Items (already newest first) in Today / Yesterday / ... groups; empty groups are left out.
export function groupByAge<T extends { updated_at: number }>(
  items: readonly T[],
  now: Date = new Date(),
): { group: AgeGroup; label: string; items: T[] }[] {
  const buckets = new Map<AgeGroup, T[]>()
  for (const item of items) {
    const group = ageGroup(item.updated_at, now)
    buckets.set(group, [...(buckets.get(group) ?? []), item])
  }
  return AGE_GROUP_ORDER.flatMap(({ group, label }) =>
    buckets.has(group) ? [{ group, label, items: buckets.get(group)! }] : [],
  )
}
