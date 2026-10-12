import { describe, expect, it } from "vitest"
import {
  ageGroup,
  dayDividerLabel,
  dayKey,
  fullDateTime,
  groupByAge,
  messageTime,
  startsNewDay,
  AGE_GROUP_ORDER,
} from "@/time"

// Local calendar days, never 24-hour windows: a chat from 23:50 yesterday is "Yesterday" at 00:10.
const at = (y: number, m: number, d: number, h = 12, min = 0) => new Date(y, m - 1, d, h, min).getTime() / 1000
const NOW = new Date(2026, 9, 12, 12, 0) // 12 Oct 2026, midday

describe("dayKey", () => {
  it("is the same for any time on one local day and differs between days", () => {
    expect(dayKey(at(2026, 10, 12, 0, 5))).toBe(dayKey(at(2026, 10, 12, 23, 55)))
    expect(dayKey(at(2026, 10, 12))).not.toBe(dayKey(at(2026, 10, 11)))
  })
})

describe("startsNewDay", () => {
  it("is true for the first message and when the day changes", () => {
    expect(startsNewDay(undefined, at(2026, 10, 12))).toBe(true)
    expect(startsNewDay(at(2026, 10, 12, 9), at(2026, 10, 12, 18))).toBe(false)
    expect(startsNewDay(at(2026, 10, 11, 23, 59), at(2026, 10, 12, 0, 1))).toBe(true)
  })
})

describe("dayDividerLabel", () => {
  it("names today and yesterday in the given locale and gives other days a date", () => {
    expect(dayDividerLabel(at(2026, 10, 12, 8), NOW, "en-GB").toLowerCase()).toBe("today")
    expect(dayDividerLabel(at(2026, 10, 11, 8), NOW, "en-GB").toLowerCase()).toBe("yesterday")
    expect(dayDividerLabel(at(2026, 10, 11, 8), NOW, "de-DE").toLowerCase()).toBe("gestern")
    const older = dayDividerLabel(at(2026, 9, 1), NOW, "en-GB")
    expect(older).toContain("September")
    expect(older).not.toContain("2026") // same year: the year is left out
    expect(dayDividerLabel(at(2025, 3, 2), NOW, "en-GB")).toContain("2025")
  })
})

describe("messageTime and fullDateTime", () => {
  it("format with the given locale's conventions", () => {
    const ts = at(2026, 10, 12, 15, 7)
    expect(messageTime(ts, "en-GB")).toBe("15:07")
    expect(messageTime(ts, "en-US")).toMatch(/3:07\s?PM/)
    expect(fullDateTime(ts, "en-GB")).toContain("12 October 2026")
    expect(fullDateTime(ts, "en-GB")).toContain("15:07")
  })
})

describe("ageGroup", () => {
  it("buckets by local calendar day", () => {
    expect(ageGroup(at(2026, 10, 12, 0, 1), NOW)).toBe("today")
    expect(ageGroup(at(2026, 10, 11, 23, 59), NOW)).toBe("yesterday")
    expect(ageGroup(at(2026, 10, 10), NOW)).toBe("week")
    expect(ageGroup(at(2026, 10, 5), NOW)).toBe("week") // 7 days ago
    expect(ageGroup(at(2026, 10, 4), NOW)).toBe("month") // 8 days ago
    expect(ageGroup(at(2026, 9, 12), NOW)).toBe("month") // 30 days ago
    expect(ageGroup(at(2026, 9, 11), NOW)).toBe("older")
  })

  it("puts the future in today, so a clock slip never hides a chat", () => {
    expect(ageGroup(at(2026, 10, 20), NOW)).toBe("today")
  })
})

describe("groupByAge", () => {
  const item = (id: string, ts: number) => ({ id, updated_at: ts })

  it("keeps the order given, drops empty groups and orders groups from newest to oldest", () => {
    const groups = groupByAge(
      [item("a", at(2026, 10, 12)), item("b", at(2026, 10, 11)), item("c", at(2026, 10, 12, 1)), item("d", at(2025, 1, 1))],
      NOW,
    )
    expect(groups.map((g) => g.group)).toEqual(["today", "yesterday", "older"])
    expect(groups[0]!.items.map((i) => i.id)).toEqual(["a", "c"])
  })

  it("has a label for every group", () => {
    for (const group of AGE_GROUP_ORDER) expect(group.label.length).toBeGreaterThan(0)
    expect(AGE_GROUP_ORDER.map((g) => g.label)).toEqual(["Today", "Yesterday", "Previous 7 days", "Previous 30 days", "Older"])
  })
})
