import { describe, expect, it, vi } from "vitest"
import type { Chat, ChatMessage, Library, Snapshot } from "@/api"
import type { Pending } from "@/conversion"
import { ChatSync, PAGE_MESSAGES, WINDOW_MESSAGES, type ChatPage, type HistoryWindow, type SyncApi, type SyncView } from "@/sync"

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
  const chats = new Map<string, Deferred<ChatPage>[]>()
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
    chat: vi.fn((id: string, _page?: { limit: number; before?: number; around?: number }) => {
      const next = deferred<ChatPage>()
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
    window: { hasEarlier: false, hasLater: false } as HistoryWindow,
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
    window: (w) => (shown.window = w),
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
    answerChat(id: string, messages: ChatMessage[] = [], index = 0, flags: Partial<ChatPage> = {}) {
      chats.get(id)![index]!.resolve({ chat: chat(id), messages, has_earlier: false, has_later: false, ...flags })
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

// A chat can hold thousands of turns (every voice request lands in the open chat), so the page holds a window of
// it: the newest page first, earlier pages as the owner scrolls up, never more than WINDOW_MESSAGES at once.
const run = (from: number, count: number) => Array.from({ length: count }, (_, i) => message(from + i, i % 2 ? "assistant" : "user"))

async function openedWith(t: ReturnType<typeof setup>, messages: ChatMessage[], flags: Partial<ChatPage> = {}) {
  t.start()
  await settle()
  t.polls[0]!.reply.resolve(snapshot({ rev: 1, active_chat_id: "A" }))
  await settle()
  t.answerChat("A", messages, 0, flags)
  await settle()
}

describe("a long chat", () => {
  it("is loaded newest page first, and the first poll asks for no messages it would only send again", async () => {
    const t = setup()
    await openedWith(t, run(951, 50), { has_earlier: true })
    expect(t.polls[0]!.after).toBeGreaterThan(Number.MAX_SAFE_INTEGER - 1)
    expect(t.api.chat).toHaveBeenCalledWith("A", { limit: PAGE_MESSAGES })
    expect(t.shown.window).toEqual({ hasEarlier: true, hasLater: false })
    // from then on the poll asks for what is newer than the newest message shown
    expect(t.polls.at(-1)!.after).toBe(1000)
    t.stop()
  })

  it("puts earlier messages before the shown ones, once", async () => {
    const t = setup()
    await openedWith(t, run(951, 50), { has_earlier: true })
    const loading = t.sync.loadEarlier()
    void t.sync.loadEarlier() // a second scroll to the top joins the first
    await settle()
    expect(t.api.chat).toHaveBeenLastCalledWith("A", { limit: PAGE_MESSAGES, before: 951 })
    t.answerChat("A", run(901, 50), 1, { has_earlier: false, has_later: true })
    await loading
    expect(t.api.chat).toHaveBeenCalledTimes(2)
    expect(t.shown.messages.map((m) => m.id)).toEqual(run(901, 100).map((m) => m.id))
    expect(t.shown.window).toEqual({ hasEarlier: false, hasLater: false })
    t.stop()
  })

  it("lets go of the newest messages once the window is full, and follows them again on the way back", async () => {
    const t = setup()
    await openedWith(t, run(10_001, 50), { has_earlier: true })
    let first = 10_001
    for (let page = 1; first - PAGE_MESSAGES > 0 && t.shown.messages.length < WINDOW_MESSAGES + PAGE_MESSAGES; page++) {
      const loading = t.sync.loadEarlier()
      await settle()
      first -= PAGE_MESSAGES
      t.answerChat("A", run(first, PAGE_MESSAGES), page, { has_earlier: true, has_later: true })
      await loading
    }
    expect(t.shown.messages.length).toBeLessThanOrEqual(WINDOW_MESSAGES)
    expect(t.shown.messages[0]!.id).toBe(first)
    expect(t.shown.window.hasLater).toBe(true)

    // away from the end, new turns are not added below the window and the poll does not ask for them
    t.polls.at(-1)!.reply.resolve(snapshot({ rev: 2, messages: [message(20_000, "assistant", "new")] }))
    await settle()
    expect(t.shown.messages.some((m) => m.id === 20_000)).toBe(false)
    expect(t.polls.at(-1)!.after).toBeGreaterThan(Number.MAX_SAFE_INTEGER - 1)

    const back = t.sync.jumpToLatest()
    await settle()
    expect(t.api.chat).toHaveBeenLastCalledWith("A", { limit: PAGE_MESSAGES })
    t.answerChat("A", run(19_951, 50), t.chats.get("A")!.length - 1, { has_earlier: true })
    await back
    expect(t.shown.window).toEqual({ hasEarlier: true, hasLater: false })
    expect(t.shown.messages.at(-1)!.id).toBe(20_000)
    t.stop()
  })

  it("keeps the newest messages while a message is being sent", async () => {
    const t = setup()
    await openedWith(t, run(10_001, 50), { has_earlier: true })
    t.sync.beginSend("hello")
    let first = 10_001
    for (let page = 1; page <= 8; page++) {
      const loading = t.sync.loadEarlier()
      await settle()
      first -= PAGE_MESSAGES
      t.answerChat("A", run(first, PAGE_MESSAGES), page, { has_earlier: true, has_later: true })
      await loading
    }
    expect(t.shown.messages.at(-1)!.id).toBe(10_050)
    expect(t.shown.window.hasLater).toBe(false)
    t.stop()
  })

  it("opens a search result on the stretch around the message found", async () => {
    const t = setup()
    await openedWith(t, run(951, 50), { has_earlier: true })
    const opening = t.sync.openChat("B", 42)
    await settle()
    expect(t.api.chat).toHaveBeenLastCalledWith("B", { limit: PAGE_MESSAGES, around: 42 })
    t.answerChat("B", run(17, 50), 0, { has_earlier: true, has_later: true })
    await opening
    expect(t.shown.chat?.id).toBe("B")
    expect(t.shown.window).toEqual({ hasEarlier: true, hasLater: true })
    t.stop()
  })
})
