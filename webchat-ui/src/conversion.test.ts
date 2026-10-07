import { describe, expect, it } from "vitest"
import type { ChatMessage, Notice } from "@/api"
import { buildThreadMessages, chatTitle, lastMessageId, mergeMessages } from "@/conversion"

const message = (id: number, role: "user" | "assistant", text: string, source: ChatMessage["source"] = "typed"): ChatMessage => ({
  id,
  role,
  text,
  ts: 1_760_000_000 + id,
  source,
})

const notice = (id: number, kind: string, request: string | null = null): Notice => ({
  id,
  kind,
  text: `notice ${kind}`,
  request,
  ts: 1_760_000_100,
})

describe("buildThreadMessages", () => {
  it("shows stored messages in order with their text", () => {
    const out = buildThreadMessages([message(1, "user", "hi"), message(2, "assistant", "hello")], [], null)
    expect(out.map((m) => m.role)).toEqual(["user", "assistant"])
    expect(out[0]?.content).toEqual([{ type: "text", text: "hi" }])
  })

  it("marks spoken turns as voice so they get the voice look", () => {
    const [spoken, typed] = buildThreadMessages(
      [message(1, "user", "what time is it", "voice"), message(2, "user", "typed")],
      [],
      null,
    )
    expect(spoken?.metadata?.modality).toBe("voice")
    expect(typed?.metadata?.modality).toBeUndefined()
  })

  it("gives every message its own id", () => {
    const out = buildThreadMessages([message(1, "user", "a")], [notice(1, "failed", "b")], { text: "c", accepted: true })
    const ids = out.map((m) => m.id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it("shows a request that could not be answered as the owner's message, then the notice", () => {
    const out = buildThreadMessages([], [notice(3, "failed", "open the pod bay doors")], null)
    expect(out.map((m) => m.role)).toEqual(["user", "assistant"])
    expect(out[1]?.metadata?.custom).toMatchObject({ notice: true, kind: "failed" })
  })

  it("shows a notice without a request as a single line", () => {
    expect(buildThreadMessages([], [notice(4, "busy")], null)).toHaveLength(1)
  })

  it("shows the message being sent and a thinking placeholder until the reply arrives", () => {
    const out = buildThreadMessages([], [], { text: "hello", accepted: false })
    expect(out.map((m) => m.role)).toEqual(["user", "assistant"])
    expect(out[1]?.status).toEqual({ type: "running" })
    expect(out[1]?.content).toEqual([])
  })
})

describe("mergeMessages", () => {
  it("appends only what is new", () => {
    const current = [message(1, "user", "a"), message(2, "assistant", "b")]
    const merged = mergeMessages(current, [message(2, "assistant", "b"), message(3, "user", "c")])
    expect(merged.map((m) => m.id)).toEqual([1, 2, 3])
  })

  it("keeps the same list when nothing is new", () => {
    const current = [message(1, "user", "a")]
    expect(mergeMessages(current, [])).toBe(current)
    expect(mergeMessages(current, [message(1, "user", "a")])).toBe(current)
  })
})

describe("lastMessageId", () => {
  it("is the highest id, or 0 for an empty chat", () => {
    expect(lastMessageId([])).toBe(0)
    expect(lastMessageId([message(5, "user", "a"), message(2, "user", "b")])).toBe(5)
  })
})

describe("chatTitle", () => {
  it("names an untitled chat", () => {
    expect(chatTitle({ title: "   " })).toBe("New chat")
    expect(chatTitle({ title: "Rome essay" })).toBe("Rome essay")
  })
})
