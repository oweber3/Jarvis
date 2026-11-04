import type { AttachmentMarker, Chat, ChatMessage, ChatPage, Library, Notice, Snapshot } from "@/api"
import { lastMessageId, mergeMessages, pendingDone, type Pending } from "@/conversion"
import type { LiveStep } from "@/steps"

// Keeps the page in step with the server while the owner acts at the same time. The server owns the
// conversation; this follows it with a long poll and applies the owner's own actions on top, so the two
// can never leave the page showing something the server is not doing. No React in here: the page hands
// in what to call to show things (`SyncView`), which is also what makes the ordering rules testable.

export type { ChatPage }
export type PageRequest = { limit: number; before?: number; around?: number }

export type SyncApi = {
  poll: (rev: number, after: number, signal: AbortSignal) => Promise<Snapshot>
  library: () => Promise<Library>
  chat: (id: string, page?: PageRequest) => Promise<ChatPage>
  openChat: (id: string) => Promise<unknown>
  createChat: (projectId: string | null) => Promise<{ chat: Chat }>
}

// What the page holds of the open chat: whether there is more before the first message shown, and whether the
// window stops short of the newest message (the owner scrolled far back), when new turns are not added below it.
export type HistoryWindow = { hasEarlier: boolean; hasLater: boolean }

/** How many messages a chat is loaded with, and how many each step back adds. */
export const PAGE_MESSAGES = 50
/** The most messages the page holds at once, however far back the owner scrolls. */
export const WINDOW_MESSAGES = 300
// The poll's "newer than" while no window reaches the newest message: the server then sends none.
const NOTHING_NEWER = Number.MAX_SAFE_INTEGER

export type Meta = Pick<Snapshot, "ready" | "state" | "busy" | "busy_query" | "mode" | "model" | "cloud">

export type SyncView = {
  /** Every snapshot as it arrives, before it is applied (the phone app reads its own part of it). */
  snapshot?: (snapshot: Snapshot) => void
  library: (library: Library) => void
  chat: (chat: Chat | null) => void
  messages: (messages: ChatMessage[]) => void
  notices: (notices: Notice[]) => void
  meta: (meta: Meta) => void
  loading: (loading: boolean) => void
  pending: (pending: Pending | null) => void
  reachable: (reachable: boolean) => void
  window: (window: HistoryWindow) => void
  /** The steps taken so far for the typed request in flight. */
  liveSteps?: (steps: LiveStep[]) => void
  /** Whether Stop was pressed for a request the server still reports as running. */
  stopping?: (stopping: boolean) => void
}

const BACKOFF_START_MS = 1000
const BACKOFF_MAX_MS = 15000
// A snapshot that could not be trusted is replaced by a fresh one after this short pause.
const REFETCH_PAUSE_MS = 250

const sleepFor = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

export class ChatSync {
  private rev = -1
  private libraryRev = -1
  private historyRev: number | null = null
  private liveKey = "[]"
  private polls = 0
  private loads = 0
  private switching = 0
  private switchedAt = 0
  private refetch = false
  private active: string | null = null
  private shown: ChatMessage[] = []
  private hasEarlier = false
  private hasLater = false
  private earlier: Promise<void> | null = null
  private sending: Pending | null = null
  // Stop was pressed and the server has not yet said the request ended.
  private stopping = false

  constructor(
    private readonly api: SyncApi,
    private readonly view: SyncView,
    private readonly sleep: (ms: number) => Promise<void> = sleepFor,
  ) {}

  get activeId(): string | null {
    return this.active
  }

  get messages(): readonly ChatMessage[] {
    return this.shown
  }

  get pending(): Pending | null {
    return this.sending
  }

  /** Whether the window stops short of the newest message, so new turns are not being added to it. */
  get detached(): boolean {
    return this.hasLater
  }

  // -- the long poll ---------------------------------------------------------------------

  async run(signal: AbortSignal): Promise<void> {
    let backoff = BACKOFF_START_MS
    while (!signal.aborted) {
      const poll = ++this.polls
      try {
        await this.apply(await this.api.poll(this.rev, this.newerThan(), signal), poll)
        this.view.reachable(true)
        backoff = BACKOFF_START_MS
        if (this.refetch) await this.sleep(REFETCH_PAUSE_MS)
      } catch {
        if (signal.aborted) return
        this.view.reachable(false)
        await this.sleep(backoff)
        backoff = Math.min(backoff * 2, BACKOFF_MAX_MS)
      }
    }
  }

  // ``poll`` is the number of the poll the snapshot came from. The snapshot was made when the server
  // answered, which can be before something the owner did since that poll was sent.
  private async apply(snap: Snapshot, poll: number): Promise<void> {
    this.refetch = false
    this.view.snapshot?.(snap)
    this.view.meta({
      ready: snap.ready,
      state: snap.state,
      busy: snap.busy,
      busy_query: snap.busy_query,
      mode: snap.mode,
      model: snap.model,
      cloud: snap.cloud,
    })
    this.view.notices(snap.notices)
    const live = snap.live_steps ?? []
    const liveKey = JSON.stringify(live)
    if (liveKey !== this.liveKey) {
      this.liveKey = liveKey
      this.view.liveSteps?.(live)
    }
    // Remembered only once the library was fetched, so a failed fetch is retried with the same snapshot. It is
    // fetched while the chat loads, not before.
    const libraryRev = snap.library_rev
    let libraryFailure: { error: unknown } | null = null
    const library =
      libraryRev !== this.libraryRev
        ? this.refreshLibrary().then(
            () => {
              this.libraryRev = libraryRev
            },
            (error: unknown) => {
              libraryFailure = { error }
            },
          )
        : null
    // Messages were taken back (a rewind, here or on another page): what is shown may hold them.
    const rewound = snap.history_rev !== undefined && this.historyRev !== null && snap.history_rev !== this.historyRev
    if (snap.history_rev !== undefined) this.historyRev = snap.history_rev
    if (this.chatChangedSince(poll)) {
      // The owner switched chats after this poll was sent: it may still name the chat they left.
      this.refetch = true
    } else if (rewound && snap.active_chat_id !== null && snap.active_chat_id === this.active) {
      await this.loadChat(snap.active_chat_id)
    } else if (snap.active_chat_id !== this.active) {
      if (snap.active_chat_id) await this.loadChat(snap.active_chat_id)
      else {
        this.active = null
        this.view.chat(null)
        this.setWindow(false, false)
        this.showMessages([])
        this.view.loading(false)
      }
    } else {
      if (snap.chat) this.view.chat(snap.chat)
      if (!this.hasLater) this.showMessages(mergeMessages(this.shown, snap.messages))
    }
    if (library) {
      await library
      if (libraryFailure) throw (libraryFailure as { error: unknown }).error
    }
    if (this.stopping && !snap.busy_query) {
      this.stopping = false
      this.view.stopping?.(false)
    }
    // The reply is stored before the query reads done, so the thinking placeholder can go.
    if (pendingDone(this.sending, snap.busy_query, this.shown, poll)) this.setPending(null)
    else if (this.sending?.accepted && !snap.busy_query && poll <= this.sending.acceptedAt) this.refetch = true
    this.rev = this.refetch ? -1 : snap.rev
  }

  private chatChangedSince(poll: number): boolean {
    return this.switching > 0 || poll <= this.switchedAt
  }

  // The poll brings the open chat's turns newer than this: the newest one shown, or none at all while no chat is
  // loaded yet (it is loaded on its own) or the window stops short of the newest message.
  private newerThan(): number {
    return this.active === null || this.hasLater ? NOTHING_NEWER : lastMessageId(this.shown)
  }

  // -- what the owner does -----------------------------------------------------------------

  async refreshLibrary(): Promise<void> {
    this.view.library(await this.api.library())
  }

  /** Loads a chat's newest page, or the stretch ``around`` a message. Only the latest load applies, so a slow
   * earlier one cannot overwrite it. */
  async loadChat(id: string, around?: number): Promise<void> {
    const ticket = ++this.loads
    this.view.loading(true)
    try {
      const loaded = await this.api.chat(id, around === undefined ? { limit: PAGE_MESSAGES } : { limit: PAGE_MESSAGES, around })
      if (ticket !== this.loads) return
      this.active = id
      this.earlier = null
      this.view.chat(loaded.chat)
      this.setWindow(!!loaded.has_earlier, !!loaded.has_later)
      this.showMessages(loaded.messages)
    } finally {
      if (ticket === this.loads) this.view.loading(false)
    }
  }

  /** Puts the page before the first message shown in front of it. Calls made while one is loading join it. */
  loadEarlier(): Promise<void> {
    if (!this.earlier && this.active !== null && this.hasEarlier && this.shown.length > 0) {
      this.earlier = this.fetchEarlier(this.active, this.loads).finally(() => {
        this.earlier = null
      })
    }
    return this.earlier ?? Promise.resolve()
  }

  private async fetchEarlier(id: string, ticket: number): Promise<void> {
    const page = await this.api.chat(id, { limit: PAGE_MESSAGES, before: this.shown[0]!.id })
    if (ticket !== this.loads || id !== this.active) return
    const known = new Set(this.shown.map((m) => m.id))
    let next = [...page.messages.filter((m) => !known.has(m.id)), ...this.shown]
    let later = this.hasLater
    // A full window lets go of its newest messages, unless a message is on its way: its reply belongs below them.
    if (next.length > WINDOW_MESSAGES && this.sending === null) {
      next = next.slice(0, WINDOW_MESSAGES)
      later = true
    }
    this.setWindow(!!page.has_earlier, later)
    this.showMessages(next)
  }

  /** Back to the newest page of the open chat, after the window let go of it. */
  async jumpToLatest(): Promise<void> {
    if (this.active !== null) await this.loadChat(this.active)
  }

  /** Opens a chat on its newest page, or on the stretch ``around`` a message (a search result). */
  async openChat(id: string, around?: number): Promise<void> {
    await this.switchChats(async () => {
      await this.api.openChat(id)
      await this.loadChat(id, around)
    })
  }

  async newChat(projectId: string | null): Promise<void> {
    await this.switchChats(async () => {
      const created = await this.api.createChat(projectId)
      await Promise.all([this.loadChat(created.chat.id), this.refreshLibrary()])
    })
  }

  // While the owner's switch is in flight, and for polls sent before it finished, the server's answer
  // about the open chat is not trusted.
  private async switchChats(work: () => Promise<void>): Promise<void> {
    this.switching++
    try {
      await work()
      this.view.notices([])
    } finally {
      this.switching--
      this.switchedAt = this.polls
    }
  }

  beginSend(text: string, attachments: readonly AttachmentMarker[] = []): void {
    this.setPending({
      text,
      accepted: false,
      afterId: lastMessageId(this.shown),
      acceptedAt: 0,
      ...(attachments.length ? { attachments: [...attachments] } : {}),
    })
  }

  /** A stored message asked again: it and what followed leave the thread at once and it shows as being sent. */
  beginRewind(messageId: number, text: string): void {
    this.showMessages(this.shown.filter((m) => m.id < messageId))
    this.setWindow(this.hasEarlier, false)
    this.beginSend(text)
  }

  /** A refused rewind changed nothing on the server: the thread shows the chat as it is again. */
  async rewindFailed(): Promise<void> {
    this.setPending(null)
    if (this.active !== null) await this.loadChat(this.active)
  }

  /** Stop was pressed: the words stay in the thread until the server's notice takes their place, and the page no
   * longer shows Jarvis working on them. */
  stopRequested(): void {
    if (this.sending) this.setPending({ ...this.sending, stopped: true })
    this.stopping = true
    this.view.stopping?.(true)
  }

  sendAccepted(): void {
    if (this.sending) this.setPending({ ...this.sending, accepted: true, acceptedAt: this.polls })
  }

  sendFailed(): void {
    this.setPending(null)
  }

  clearPending(): void {
    this.setPending(null)
  }

  private setPending(next: Pending | null): void {
    this.sending = next
    this.view.pending(next)
  }

  private showMessages(next: ChatMessage[]): void {
    this.shown = next
    this.view.messages(next)
  }

  private setWindow(hasEarlier: boolean, hasLater: boolean): void {
    if (hasEarlier === this.hasEarlier && hasLater === this.hasLater) return
    this.hasEarlier = hasEarlier
    this.hasLater = hasLater
    this.view.window({ hasEarlier, hasLater })
  }
}
