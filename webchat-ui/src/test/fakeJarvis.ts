// An in-memory Jarvis behind `fetch`, for tests that render the page in jsdom: the routes the page calls, the
// long poll (answered at once when something changed, otherwise held until something does), and a scripted
// reply engine. It keeps the server's rules that the page depends on, nothing more.

import type { Chat, ChatMessage, Library, Notice, Snapshot, ToolStep } from "@/api"

type Held = { text: string }

export type FakeOptions = { chats?: { id: string; title?: string; messages: number; projectId?: string | null }[] }

const now = () => Date.now() / 1000

export class FakeJarvis {
  rev = 1
  libraryRev = 1
  historyRev = 0
  ready = true
  busyQuery = false
  busyVoice = false
  /** How the next typed request ends: a reply, `busy` (refused), `hold` (until `finish()`), or `fail`. */
  next: "reply" | "busy" | "hold" | "fail" = "reply"
  replyText = "Done."
  readonly requests: { method: string; path: string; body: unknown }[] = []
  readonly sent: string[] = []
  readonly rewinds: { chat: string; message_id: number; text: string | undefined; newest_id: number | undefined }[] = []
  readonly modelChoices: { kind: string; value: string; effort?: string }[] = []
  localModels = [
    { id: "gemma", name: "Gemma 4 12B", installed: true },
    { id: "qwen", name: "Qwen 3.5 9B", installed: true },
    { id: "big", name: "Huge 70B", installed: false },
  ]
  currentModel = "gemma"
  /** The steps of the request in flight, as the hub serves them. */
  liveSteps: { tool: string; status: string; label: string }[] = []
  chats: Chat[] = []
  messages = new Map<string, ChatMessage[]>()
  notices: Notice[] = []
  active: string | null = null
  private nextId = 1
  private nextNotice = 1
  private nextQuery = 1
  private held: Held | null = null
  private waiters: (() => void)[] = []

  constructor(options: FakeOptions = {}) {
    for (const spec of options.chats ?? [{ id: "c1", title: "Voice and quick questions", messages: 4 }]) {
      this.addChat(spec.id, spec.title ?? "", spec.projectId ?? null)
      for (let i = 0; i < spec.messages; i++) {
        this.store(spec.id, i % 2 === 0 ? "user" : "assistant", i % 2 === 0 ? `Question ${i / 2}` : `Answer ${(i - 1) / 2}`,
          { quiet: true })
      }
    }
    this.active = this.chats[0]?.id ?? null
  }

  addChat(id: string, title = "", projectId: string | null = null): Chat {
    const chat: Chat = {
      id,
      project_id: projectId,
      title,
      created_at: now(),
      updated_at: now(),
      last_mode: "",
      last_model: "",
      pinned: false,
    }
    this.chats.unshift(chat)
    this.messages.set(id, [])
    return chat
  }

  /** A stored turn of a chat, as the hub mirrors one from the dialogue memory. */
  store(
    chatId: string,
    role: "user" | "assistant",
    text: string,
    extra: { source?: ChatMessage["source"]; steps?: ToolStep[]; quiet?: boolean; ts?: number } = {},
  ): ChatMessage {
    const message: ChatMessage = {
      id: this.nextId++,
      role,
      text,
      ts: extra.ts ?? now(),
      source: extra.source ?? "typed",
      steps: extra.steps ?? [],
      attachments: [],
    }
    this.messages.get(chatId)!.push(message)
    const chat = this.chats.find((c) => c.id === chatId)
    if (chat && !chat.title && role === "user") chat.title = text.slice(0, 60)
    if (!extra.quiet) this.bump()
    return message
  }

  /** A spoken exchange landing in the open chat. */
  voice(user: string, reply: string): void {
    this.store(this.active!, "user", user, { source: "voice", quiet: true })
    this.store(this.active!, "assistant", reply, { source: "voice" })
  }

  /** Ends a held request with the reply, or with nothing (`null`, as a stop does). */
  finish(reply: string | null = this.replyText, steps: ToolStep[] = []): void {
    const held = this.held
    this.held = null
    this.busyQuery = false
    if (!held) return
    if (reply === null) this.notice("stopped", "Stopped.", held.text)
    else {
      this.store(this.active!, "user", held.text, { quiet: true })
      this.store(this.active!, "assistant", reply, { steps, quiet: true })
    }
    this.bump()
  }

  notice(kind: string, text: string, request: string | null = null): void {
    this.notices.push({ id: this.nextNotice++, kind, text, request, ts: now() })
    this.bump()
  }

  bump(): void {
    this.rev++
    const waiting = this.waiters
    this.waiters = []
    waiting.forEach((wake) => wake())
  }

  // -- the page's door -----------------------------------------------------------------------------------

  readonly fetch = async (input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
    // The page only ever asks its own origin, by path.
    const [pathname = "", search = ""] = String(input).split("?")
    const url = { pathname, search: search ? `?${search}` : "", searchParams: new URLSearchParams(search) }
    const method = (init.method ?? "GET").toUpperCase()
    const body = typeof init.body === "string" && init.body ? JSON.parse(init.body) : undefined
    this.requests.push({ method, path: url.pathname + url.search, body })
    const route = `${method} ${url.pathname}`
    const json = (status: number, payload: unknown) =>
      new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } })
    const refuse = (status: number, error: string) => json(status, { error })

    if (route === "GET /api/poll") return json(200, await this.poll(url, init.signal ?? undefined))
    if (route === "GET /api/library") return json(200, this.library())
    if (route === "GET /api/models")
      return json(200, {
        mode: { mode: "local", enabled: ["local"] },
        current: this.currentModel,
        switchable: true,
        models: this.localModels,
        cloud: null,
      })
    if (route === "POST /api/model") {
      const choice = body as { kind: string; value: string; effort?: string }
      this.modelChoices.push(choice)
      if (choice.kind === "local") this.currentModel = choice.value
      this.bump()
      return json(200, { ok: true })
    }
    if (route === "GET /api/attachments/support")
      return json(200, {
        images: { ok: true, reason: null },
        screenshot: { ok: false, reason: "screenshot_unavailable" },
        limits: { max_upload_bytes: 10485760, max_attachments: 4, max_images: 3, max_file_chars: 20000 },
      })
    if (route === "GET /api/suggestions") return json(200, { cards: [] })
    if (route === "GET /api/mic") return json(200, { available: false, reason: "unavailable", max_seconds: 120 })
    if (route === "POST /api/stop") {
      if (this.held) this.finish(null)
      return json(200, { ok: true })
    }
    if (route === "POST /api/chat") return this.send(body as { text: string }, json, refuse)
    if (route === "POST /api/chats") {
      const chat = this.addChat(`c${this.chats.length + 1}x`)
      this.open(chat.id)
      return json(201, { chat })
    }
    const chatRoute = url.pathname.match(/^\/api\/chats\/([0-9a-z]+)(\/open|\/rewind)?$/)
    if (chatRoute) {
      const [, id, action] = chatRoute
      const chat = this.chats.find((c) => c.id === id)
      if (!chat) return refuse(404, "unknown")
      if (method === "GET" && !action) return json(200, this.page(chat, url.searchParams))
      if (method === "POST" && action === "/open") {
        this.open(chat.id)
        return json(200, { chat })
      }
      if (method === "POST" && action === "/rewind")
        return this.rewind(chat, body as { message_id: number; text?: string; newest_id?: number }, json, refuse)
      if (method === "PATCH") return json(200, { ok: true })
    }
    return refuse(404, "not_found")
  }

  // -- routes ---------------------------------------------------------------------------------------------

  private library(): Library {
    return { projects: [], chats: this.chats.map((c) => ({ ...c })), active_chat_id: this.active }
  }

  private page(chat: Chat, params: URLSearchParams) {
    const all = this.messages.get(chat.id) ?? []
    const limit = params.has("limit") ? Number(params.get("limit")) : Infinity
    let from = 0
    let to = all.length
    if (params.has("before")) {
      to = all.findIndex((m) => m.id >= Number(params.get("before")))
      if (to < 0) to = all.length
      from = Math.max(0, to - limit)
    } else if (params.has("around")) {
      const at = Math.max(0, all.findIndex((m) => m.id >= Number(params.get("around"))))
      from = Math.max(0, at - Math.floor(limit / 2))
      to = Math.min(all.length, from + limit)
      from = Math.max(0, to - limit)
    } else if (Number.isFinite(limit)) from = Math.max(0, all.length - limit)
    return { chat, messages: all.slice(from, to), has_earlier: from > 0, has_later: to < all.length }
  }

  private open(id: string): void {
    this.active = id
    this.notices = []
    this.libraryRev++
    this.bump()
  }

  private send(body: { text: string }, json: (s: number, p: unknown) => Response, refuse: (s: number, e: string) => Response) {
    this.sent.push(body.text)
    if (!this.ready) return refuse(503, "unavailable")
    if (this.next === "busy" || this.busyQuery || this.busyVoice) {
      this.notice("busy", "Jarvis is busy with another request.")
      return refuse(409, "busy")
    }
    const queryId = this.nextQuery++
    this.busyQuery = true
    this.bump()
    if (this.next === "hold") this.held = { text: body.text }
    else if (this.next === "fail") {
      this.busyQuery = false
      this.notice("failed", "Jarvis couldn't answer that.", body.text)
    } else {
      // Answered a moment later, as the engine does.
      this.held = { text: body.text }
      setTimeout(() => this.finish(this.replyText), 5)
    }
    return json(202, { query_id: queryId, status: "accepted" })
  }

  private rewind(
    chat: Chat,
    body: { message_id: number; text?: string; newest_id?: number },
    json: (s: number, p: unknown) => Response,
    refuse: (s: number, e: string) => Response,
  ) {
    this.rewinds.push({ chat: chat.id, message_id: body.message_id, text: body.text, newest_id: body.newest_id })
    if (typeof body.newest_id !== "number") return refuse(400, "bad_newest")
    if (chat.id !== this.active) return refuse(409, "not_open")
    const all = this.messages.get(chat.id) ?? []
    const at = all.findIndex((m) => m.id === body.message_id)
    if (at < 0) return refuse(404, "unknown")
    if (all[at]!.role !== "user") return refuse(400, "not_user")
    if (all.at(-1)?.id !== body.newest_id) return refuse(409, "changed")
    if (this.busyQuery || this.busyVoice || this.next === "busy") return refuse(409, "busy")
    const text = body.text ?? all[at]!.text
    this.messages.set(chat.id, all.slice(0, at))
    this.historyRev++
    return this.send({ text }, json, refuse)
  }

  private async poll(url: { searchParams: URLSearchParams }, signal: AbortSignal | undefined): Promise<Snapshot> {
    const rev = Number(url.searchParams.get("rev"))
    const after = Number(url.searchParams.get("after"))
    const newer = () => (this.messages.get(this.active ?? "") ?? []).some((m) => m.id > after)
    if (rev === this.rev && !newer()) {
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(resolve, 2000)
        this.waiters.push(() => {
          clearTimeout(timer)
          resolve()
        })
        signal?.addEventListener("abort", () => {
          clearTimeout(timer)
          reject(new DOMException("aborted", "AbortError"))
        })
      })
    }
    const messages = (this.messages.get(this.active ?? "") ?? []).filter((m) => m.id > after)
    const chat = this.chats.find((c) => c.id === this.active) ?? null
    return {
      rev: this.rev,
      library_rev: this.libraryRev,
      history_rev: this.historyRev,
      ready: this.ready,
      state: this.busyQuery ? "thinking" : "idle",
      busy: this.busyQuery || this.busyVoice,
      busy_query: this.busyQuery,
      active_chat_id: this.active,
      chat,
      messages,
      notices: [...this.notices],
      live_steps: this.busyQuery ? [...this.liveSteps] : [],
      mode: { mode: "local", enabled: ["local"] },
      model: { current: this.currentModel, switchable: true },
      cloud: null,
    } as Snapshot
  }
}
