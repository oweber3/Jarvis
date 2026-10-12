import { describe, expect, it, vi } from "vitest"
import type { Chat, ChatMessage, Library, Snapshot } from "@/api"
import type { Pending } from "@/conversion"
import { ChatSync, type SyncApi, type SyncView } from "@/sync"

// The page follows the server with a long poll while the owner acts at the same time. These tests drive
// ChatSync with a scripted server, so they check what the page ends up showing, not how it gets there.

type Deferred<T> = { promise: Promise<T>; resolve: (value: T) => void; reject: (error: unknown) => void }
function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0))
async function settle() {
  for (let i = 0; i < 8; i++) await tick()
}

const chat = (id: string): Chat => ({
  id,
  project_id: null,
  title: id,
  created_at: 1,
  updated_at: 1,
  last_mode: "",
  last_model: "",
  pinned: false,
})
const message = (id: number, role: "user" | "assistant", text = "t"): ChatMessage => ({
  id,
  role,
  text,
  ts: 1_760_000_000 + id,
  source: "typed",
  steps: [],
})
const library = (active: string | null = "A"): Library => ({ projects: [], chats: [chat("A"), chat("B")], active_chat_id: active })

function snapshot(over: Partial<Snapshot> = {}): Snapshot {
  const active = over.active_chat_id === undefined ? "A" : over.active_chat_id
  return {
    rev: 1,
    library_rev: 1,
    ready: true,
    state: "idle",
    busy: false,
    busy_query: false,
    active_chat_id: active,
    chat: active ? chat(active) : null,
    messages: [],
    notices: [],
    mode: null,
    model: { current: null, switchable: false },
    cloud: null,
    ...over,
  }
}

// ``holdLibrary``: library fetches wait for the test to answer them (to control their timing or failure).
function setup(holdLibrary = false) {
  const polls: Array<{ rev: number; after: number; reply: Deferred<Snapshot> }> = []
  const chats = new Map<string, Deferred<{ chat: Chat; messages: ChatMessage[] }>[]>()
  const libraries: Array<Deferred<Library>> = []
  const api = {
    poll: vi.fn((rev: number, after: number, signal: AbortSignal) => {
      const reply = deferred<Snapshot>()
      signal.addEventListener("abort", () => reply.reject(new Error("aborted")))
      polls.push({ rev, after, reply })
      return reply.promise
    }),
    library: vi.fn(() => {
      if (!holdLibrary) return Promise.resolve(library())
      const next = deferred<Library>()
      libraries.push(next)
      return next.promise
    }),
    chat: vi.fn((id: string) => {
      const next = deferred<{ chat: Chat; messages: ChatMessage[] }>()
      chats.set(id, [...(chats.get(id) ?? []), next])
      return next.promise
    }),
    openChat: vi.fn(async (_id: string) => ({ chat: chat(_id) })),
    createChat: vi.fn(async (_project: string | null) => ({ chat: chat("NEW") })),
  } satisfies SyncApi

  const shown = {
    library: null as Library | null,
    chat: null as Chat | null,
    messages: [] as ChatMessage[],
    pending: null as Pending | null,
    reachable: true,
  }
  const view: SyncView = {
    library: (l) => (shown.library = l),
    chat: (c) => (shown.chat = c),
    messages: (m) => (shown.messages = m),
    notices: () => undefined,
    meta: () => undefined,
    loading: () => undefined,
    pending: (p) => (shown.pending = p),
    reachable: (r) => (shown.reachable = r),
  }
  const sync = new ChatSync(api, view, async () => undefined)
  const abort = new AbortController()
  return {
    api,
    sync,
    shown,
    polls,
    chats,
    libraries,
    start: () => void sync.run(abort.signal),
    stop: () => abort.abort(),
    answerChat(id: string, messages: ChatMessage[] = [], index = 0) {
      chats.get(id)![index]!.resolve({ chat: chat(id), messages })
    },
  }
}

describe("a failed library refresh", () => {
  it("is retried instead of being marked as done", async () => {
    const t = setup(true)
    t.start()
    await settle()
    t.polls[0]!.reply.resolve(snapshot({ rev: 4, library_rev: 5 }))
    await settle()
    t.libraries[0]!.reject(new Error("offline"))
    await settle()

    // the same state is asked for again straight away, and the library is fetched again
    expect(t.polls).toHaveLength(2)
    expect(t.polls[1]!.rev).toBe(-1)
    expect(t.shown.library).toBeNull()
    t.polls[1]!.reply.resolve(snapshot({ rev: 4, library_rev: 5 }))
    await settle()
    expect(t.api.library).toHaveBeenCalledTimes(2)
    t.libraries[1]!.resolve(library())
    await settle()
    t.answerChat("A")
    await settle()
    expect(t.shown.library?.chats).toHaveLength(2)

    // once it worked, an unchanged library_rev is not fetched again
    t.polls[2]!.reply.resolve(snapshot({ rev: 5, library_rev: 5 }))
    await settle()
    expect(t.api.library).toHaveBeenCalledTimes(2)
    t.stop()
  })
})

describe("switching chats quickly", () => {
  it("shows the chat that was opened last, even when the earlier one answers later", async () => {
    const t = setup()
    const first = t.sync.openChat("A")
    const second = t.sync.openChat("B")
    await settle()
    t.answerChat("B", [message(2, "assistant", "from B")])
    await settle()
    t.answerChat("A", [message(1, "assistant", "from A")])
    await Promise.all([first, second])

    expect(t.shown.chat?.id).toBe("B")
    expect(t.shown.messages.map((m) => m.text)).toEqual(["from B"])
    expect(t.sync.activeId).toBe("B")
  })

  it("ignores a poll snapshot taken before the switch, which still names the old chat", async () => {
    const t = setup()
    t.start()
    await settle()
    t.polls[0]!.reply.resolve(snapshot({ rev: 1, active_chat_id: "A" }))
    await settle()
    t.answerChat("A")
    await settle()
    expect(t.sync.activeId).toBe("A")

    const opening = t.sync.openChat("B")
    await settle()
    // the poll that was waiting returns while the switch is still in flight, naming A
    t.polls.at(-1)!.reply.resolve(snapshot({ rev: 2, active_chat_id: "A" }))
    await settle()
    t.answerChat("B", [message(7, "user", "typed in B")])
    await opening
    await settle()

    expect(t.api.chat).toHaveBeenCalledTimes(2) // A once, B once; never A again
    expect(t.shown.chat?.id).toBe("B")
    expect(t.shown.messages.map((m) => m.id)).toEqual([7])
    t.stop()
  })

  it("still follows the server when a later poll says another chat is open", async () => {
    const t = setup()
    t.start()
    await settle()
    t.polls[0]!.reply.resolve(snapshot({ rev: 1, active_chat_id: "A" }))
    await settle()
    t.answerChat("A")
    await settle()
    // a voice-driven or tray-driven switch, with no owner action in flight
    t.polls.at(-1)!.reply.resolve(snapshot({ rev: 2, active_chat_id: "B" }))
    await settle()
    t.answerChat("B", [message(3, "user", "hi")])
    await settle()
    expect(t.shown.chat?.id).toBe("B")
    t.stop()
  })

  it("does not let a poll that was started before the switch finished undo it afterwards", async () => {
    const t = setup()
    t.start()
    await settle()
    t.polls[0]!.reply.resolve(snapshot({ rev: 1, active_chat_id: "A" }))
    await settle()
    t.answerChat("A")
    await settle()
    const stale = t.polls.at(-1)! // issued before the owner switches

    const opening = t.sync.openChat("B")
    await settle()
    t.answerChat("B")
    await opening
    await settle()
    stale.reply.resolve(snapshot({ rev: 2, active_chat_id: "A" })) // generated before the server switched
    await settle()

    expect(t.api.chat).toHaveBeenCalledTimes(2)
    expect(t.shown.chat?.id).toBe("B")
    // and the next poll is made at once, to learn the truth
    expect(t.polls.at(-1)!.rev).toBe(-1)
    t.stop()
  })
})

describe("the message being sent", () => {
  it("stays visible when the send is confirmed while an older snapshot is still being applied", async () => {
    const t = setup(true)
    t.start()
    await settle()
    t.sync.beginSend("hello")
    expect(t.shown.pending?.text).toBe("hello")

    // a poll made before the send returns: Jarvis is not busy yet, and the library needs refreshing first
    t.polls[0]!.reply.resolve(snapshot({ rev: 1, library_rev: 3, busy_query: false }))
    await settle()
    t.sync.sendAccepted() // api.send resolves during the library refresh
    t.libraries[0]!.resolve(library())
    await settle()
    t.answerChat("A")
    await settle()

    expect(t.shown.pending?.text).toBe("hello")
    // the next poll asks for a fresh snapshot, made after the send was confirmed
    expect(t.polls.at(-1)!.rev).toBe(-1)
    t.polls.at(-1)!.reply.resolve(snapshot({ rev: 2, library_rev: 3, busy_query: true }))
    await settle()
    expect(t.shown.pending?.text).toBe("hello")
    t.polls.at(-1)!.reply.resolve(
      snapshot({ rev: 3, library_rev: 3, busy_query: false, messages: [message(1, "user", "hello"), message(2, "assistant", "hi")] }),
    )
    await settle()
    expect(t.shown.pending).toBeNull()
    expect(t.shown.messages.map((m) => m.id)).toEqual([1, 2])
    t.stop()
  })

  it("goes when the send fails", async () => {
    const t = setup()
    t.sync.beginSend("hello")
    t.sync.sendFailed()
    expect(t.shown.pending).toBeNull()
  })
})
