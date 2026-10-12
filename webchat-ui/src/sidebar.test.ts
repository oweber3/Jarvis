import { describe, expect, it } from "vitest"
import type { Chat } from "@/api"
import { arrangeChats } from "@/sidebar"

const NOW = new Date(2026, 9, 12, 12, 0)
const at = (day: number) => new Date(2026, 9, day, 9, 0).getTime() / 1000
const chat = (id: string, day: number, extra: Partial<Chat> = {}): Chat => ({
  id,
  project_id: null,
  title: id,
  created_at: 1,
  updated_at: at(day),
  last_mode: "",
  last_model: "",
  pinned: false,
  ...extra,
})

describe("arrangeChats", () => {
  it("lists pinned chats first, once, and groups the rest by age", () => {
    const arranged = arrangeChats([chat("new", 12), chat("pin", 1, { pinned: true }), chat("old", 2)], NOW)
    expect(arranged.pinned.map((c) => c.id)).toEqual(["pin"])
    expect(arranged.groups.map((g) => [g.label, g.items.map((c) => c.id)])).toEqual([
      ["Today", ["new"]],
      ["Previous 30 days", ["old"]],
    ])
  })

  it("has no pinned section and no groups for no chats", () => {
    expect(arrangeChats([], NOW)).toEqual({ pinned: [], groups: [] })
  })

  it("keeps the order it was given inside each section", () => {
    const arranged = arrangeChats([chat("b", 12), chat("a", 12)], NOW)
    expect(arranged.groups[0]!.items.map((c) => c.id)).toEqual(["b", "a"])
  })
})
