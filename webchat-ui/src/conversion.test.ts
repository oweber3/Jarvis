import { describe, expect, it } from "vitest"
import type { ChatMessage, Notice, ToolStep } from "@/api"
import { buildThreadMessages, chatTitle, lastMessageId, mergeMessages, pendingDone } from "@/conversion"

const message = (id: number, role: "user" | "assistant", text: string, source: ChatMessage["source"] = "typed"): ChatMessage => ({
  id,
  role,
  text,
  ts: 1_760_000_000 + id,
  source,
  steps: [],
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
    const out = buildThreadMessages([message(1, "user", "a")], [notice(1, "failed", "b")], { text: "c", accepted: true, afterId: 0, acceptedAt: 1 })
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

  it("places a notice by time among the stored messages, not after all of them", () => {
    // messages at ts +1 .. +3; the notice happened between the first and second turn
    const early: Notice = { ...notice(5, "stopped"), ts: 1_760_000_001.5 }
    const out = buildThreadMessages(
      [message(1, "user", "a"), message(2, "assistant", "b"), message(3, "user", "c")],
      [early],
      null,
    )
    expect(out.map((m) => m.id)).toEqual(["m1", "n5", "m2", "m3"])
  })

  it("keeps a notice newer than every message at the end, after the message it follows", () => {
    const same: Notice = { ...notice(6, "busy"), ts: 1_760_000_002 }
    const out = buildThreadMessages([message(1, "user", "a"), message(2, "user", "b")], [same], null)
    expect(out.map((m) => m.id)).toEqual(["m1", "m2", "n6"])
  })

  it("keeps a failed request and its notice together where the notice happened", () => {
    const early: Notice = { ...notice(7, "failed", "open the doors"), ts: 1_760_000_001.5 }
    const out = buildThreadMessages([message(1, "user", "a"), message(2, "assistant", "b")], [early], null)
    expect(out.map((m) => m.id)).toEqual(["m1", "n7-request", "n7", "m2"])
  })

  it("orders several notices by time too", () => {
    const first: Notice = { ...notice(8, "busy"), ts: 1_760_000_010 }
    const second: Notice = { ...notice(9, "stopped"), ts: 1_760_000_020 }
    const out = buildThreadMessages([message(1, "user", "a")], [second, first], null)
    expect(out.map((m) => m.id)).toEqual(["m1", "n8", "n9"])
  })

  it("shows the message being sent and a thinking placeholder until the reply arrives", () => {
    const out = buildThreadMessages([], [], { text: "hello", accepted: false, afterId: 0, acceptedAt: 0 })
    expect(out.map((m) => m.role)).toEqual(["user", "assistant"])
    expect(out[1]?.status).toEqual({ type: "running" })
    expect(out[1]?.content).toEqual([])
  })
})

describe("tool steps", () => {
  const withSteps = (steps: ToolStep[]): ChatMessage => ({ ...message(2, "assistant", "Done."), steps })
  const volume: ToolStep = { tool: "systemVolume", status: "done", label: "Set the volume to 30%", summary: null }
  const search: ToolStep = { tool: "webSearch", status: "done", label: "Searched the web", summary: "3 results" }

  it("shows the steps of a reply as tool calls before its text, in order", () => {
    const [reply] = buildThreadMessages([withSteps([volume, search])], [], null)
    expect(reply?.content).toMatchObject([
      { type: "tool-call", toolName: "systemVolume", args: { label: "Set the volume to 30%", status: "done" } },
      { type: "tool-call", toolName: "webSearch", args: { label: "Searched the web", summary: "3 results" } },
      { type: "text", text: "Done." },
    ])
  })

  it("gives every step of every message its own call id", () => {
    const out = buildThreadMessages([withSteps([volume, volume]), { ...withSteps([volume]), id: 9 }], [], null)
    const ids = out.flatMap((m) => (Array.isArray(m.content) ? m.content : [])).flatMap((p) => (p.type === "tool-call" ? [p.toolCallId] : []))
    expect(ids).toHaveLength(3)
    expect(new Set(ids).size).toBe(3)
  })

  it("marks a step as finished, so the thread never shows a call still running", () => {
    const waiting: ToolStep = { tool: "localFiles", status: "waiting", label: "Local files", summary: null }
    const [reply] = buildThreadMessages([withSteps([waiting])], [], null)
    const call = (reply?.content as unknown as { type: string; result?: unknown; isError?: boolean }[])[0]
    expect(call?.result).toBeDefined()
    expect(call?.isError).toBeFalsy()
  })

  it("marks a failed or refused step as an error", () => {
    const failed: ToolStep = { tool: "tvControl", status: "failed", label: "Tv control", summary: null }
    const refused: ToolStep = { tool: "localFiles", status: "declined", label: "Local files", summary: null }
    const [reply] = buildThreadMessages([withSteps([failed, refused])], [], null)
    const calls = reply?.content as unknown as { isError?: boolean }[]
    expect(calls.slice(0, 2).map((c) => c.isError)).toEqual([true, true])
  })

  it("leaves a reply without steps as plain text", () => {
    const [reply] = buildThreadMessages([message(2, "assistant", "Hi")], [], null)
    expect(reply?.content).toEqual([{ type: "text", text: "Hi" }])
  })

  it("copes with a server that sends no steps field", () => {
    const old = { ...message(2, "assistant", "Hi") } as Partial<ChatMessage>
    delete old.steps
    const [reply] = buildThreadMessages([old as ChatMessage], [], null)
    expect(reply?.content).toEqual([{ type: "text", text: "Hi" }])
  })

  it("never adds steps to what the owner said", () => {
    const [user] = buildThreadMessages([{ ...message(1, "user", "hi"), steps: [volume] }], [], null)
    expect(user?.content).toEqual([{ type: "text", text: "hi" }])
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

describe("pendingDone", () => {
  const sent = { text: "hi", accepted: false, afterId: 4, acceptedAt: 0 }
  const accepted = { ...sent, accepted: true, acceptedAt: 3 }

  it("waits while Jarvis is still working on the request", () => {
    expect(pendingDone(accepted, true, [], 4)).toBe(false)
  })

  it("is done once the send was confirmed and a poll made after that shows Jarvis has finished", () => {
    expect(pendingDone(accepted, false, [], 4)).toBe(true)
  })

  it("ignores a snapshot from a poll made before the send was confirmed, which cannot know about the request", () => {
    expect(pendingDone(accepted, false, [], 3)).toBe(false)
    expect(pendingDone(accepted, false, [], 2)).toBe(false)
  })

  it("is done when a fast reply is already stored, even before the send was confirmed", () => {
    expect(pendingDone(sent, false, [message(5, "user", "hi"), message(6, "assistant", "hello")], 1)).toBe(true)
  })

  it("does not take an older reply for the answer", () => {
    expect(pendingDone(sent, false, [message(3, "assistant", "earlier")], 1)).toBe(false)
  })

  it("has nothing to clear without a message being sent", () => {
    expect(pendingDone(null, false, [], 1)).toBe(false)
  })
})
