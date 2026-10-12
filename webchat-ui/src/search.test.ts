import { describe, expect, it } from "vitest"
import type { Chat, SearchHit } from "@/api"
import { highlightParts, searchRows } from "@/search"

const chat = (id: string, title: string, extra: Partial<Chat> = {}): Chat => ({
  id,
  project_id: null,
  title,
  created_at: 1,
  updated_at: 1,
  last_mode: "",
  last_model: "",
  pinned: false,
  ...extra,
})
const hit = (chat_id: string, message_id: number, snippet = "…text…"): SearchHit => ({ chat_id, message_id, snippet, ts: 1 })

describe("searchRows", () => {
  const chats = [chat("a", "Train times"), chat("b", "Dinner ideas"), chat("c", "")]

  it("lists chats whose title matches, ignoring case", () => {
    expect(searchRows(chats, "TRAIN", []).map((r) => r.chat.id)).toEqual(["a"])
  })

  it("adds chats found by message text, with the snippet and message to scroll to", () => {
    const rows = searchRows(chats, "pasta", [hit("b", 7, "…some pasta…")])
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({ chat: { id: "b" }, messageId: 7, snippet: "…some pasta…" })
  })

  it("shows a chat once, with its snippet, when title and text both match", () => {
    const rows = searchRows(chats, "train", [hit("a", 3, "the train")])
    expect(rows.map((r) => r.chat.id)).toEqual(["a"])
    expect(rows[0]!.messageId).toBe(3)
  })

  it("keeps the library's order and ignores hits for chats it does not know", () => {
    const rows = searchRows(chats, "x", [hit("zzz", 1), hit("b", 2), hit("a", 5)])
    expect(rows.map((r) => r.chat.id)).toEqual(["a", "b"])
  })

  it("matches the placeholder title of an untitled chat", () => {
    expect(searchRows(chats, "new chat", []).map((r) => r.chat.id)).toEqual(["c"])
  })

  it("lists nothing for a blank query", () => {
    expect(searchRows(chats, "  ", [hit("a", 1)])).toEqual([])
  })
})

describe("highlightParts", () => {
  it("splits text so the match can be marked, ignoring case and keeping the original spelling", () => {
    expect(highlightParts("Which Train goes", "train")).toEqual([
      { text: "Which ", match: false },
      { text: "Train", match: true },
      { text: " goes", match: false },
    ])
  })

  it("marks every occurrence and treats the query as plain text", () => {
    expect(highlightParts("a.b a.b", "a.b").filter((p) => p.match)).toHaveLength(2)
    expect(highlightParts("axb", "a.b")).toEqual([{ text: "axb", match: false }])
  })

  it("returns the text unmarked for a blank query", () => {
    expect(highlightParts("hello", " ")).toEqual([{ text: "hello", match: false }])
  })
})
