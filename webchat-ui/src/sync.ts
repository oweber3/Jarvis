import type { Chat, ChatMessage, Library, Notice, Snapshot } from "@/api"
import { lastMessageId, mergeMessages, pendingDone, type Pending } from "@/conversion"

// Keeps the page in step with the server while the owner acts at the same time. The server owns the
// conversation; this follows it with a long poll and applies the owner's own actions on top, so the two
// can never leave the page showing something the server is not doing. No React in here: the page hands
// in what to call to show things (`SyncView`), which is also what makes the ordering rules testable.

export type SyncApi = {
  poll: (rev: number, after: number, signal: AbortSignal) => Promise<Snapshot>
  library: () => Promise<Library>
  chat: (id: string) => Promise<{ chat: Chat; messages: ChatMessage[] }>
  openChat: (id: string) => Promise<unknown>
  createChat: (projectId: string | null) => Promise<{ chat: Chat }>
}

export type Meta = Pick<Snapshot, "ready" | "busy" | "busy_query" | "mode" | "model" | "cloud">

export type SyncView = {
  library: (library: Library) => void
  chat: (chat: Chat | null) => void
  messages: (messages: ChatMessage[]) => void
  notices: (notices: Notice[]) => void
  meta: (meta: Meta) => void
  loading: (loading: boolean) => void
  pending: (pending: Pending | null) => void
  reachable: (reachable: boolean) => void
}

const BACKOFF_START_MS = 1000
const BACKOFF_MAX_MS = 15000
// A snapshot that could not be trusted is replaced by a fresh one after this short pause.
const REFETCH_PAUSE_MS = 250

const sleepFor = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

export class ChatSync {
  private rev = -1
  private libraryRev = -1
  private polls = 0
  private loads = 0
  private switching = 0
  private switchedAt = 0
  private refetch = false
  private active: string | null = null
  private shown: ChatMessage[] = []
  private sending: Pending | null = null

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

  // -- the long poll ---------------------------------------------------------------------

  async run(signal: AbortSignal): Promise<void> {
    let backoff = BACKOFF_START_MS
    while (!signal.aborted) {
      const poll = ++this.polls
      try {
        await this.apply(await this.api.poll(this.rev, lastMessageId(this.shown), signal), poll)
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
    this.view.meta({
      ready: snap.ready,
      busy: snap.busy,
      busy_query: snap.busy_query,
      mode: snap.mode,
      model: snap.model,
      cloud: snap.cloud,
    })
    this.view.notices(snap.notices)
    // Remembered only once the library was fetched, so a failed fetch is retried with the same snapshot.
    if (snap.library_rev !== this.libraryRev) {
      await this.refreshLibrary()
      this.libraryRev = snap.library_rev
    }
    if (this.chatChangedSince(poll)) {
      // The owner switched chats after this poll was sent: it may still name the chat they left.
      this.refetch = true
    } else if (snap.active_chat_id !== this.active) {
      if (snap.active_chat_id) await this.loadChat(snap.active_chat_id)
      else {
        this.active = null
        this.view.chat(null)
        this.showMessages([])
        this.view.loading(false)
      }
    } else {
      if (snap.chat) this.view.chat(snap.chat)
      this.showMessages(mergeMessages(this.shown, snap.messages))
    }
    // The reply is stored before the query reads done, so the thinking placeholder can go.
    if (pendingDone(this.sending, snap.busy_query, this.shown, poll)) this.setPending(null)
    else if (this.sending?.accepted && !snap.busy_query && poll <= this.sending.acceptedAt) this.refetch = true
    this.rev = this.refetch ? -1 : snap.rev
  }

  private chatChangedSince(poll: number): boolean {
    return this.switching > 0 || poll <= this.switchedAt
  }

  // -- what the owner does -----------------------------------------------------------------

  async refreshLibrary(): Promise<void> {
    this.view.library(await this.api.library())
  }

  /** Loads a chat's messages. Only the latest load applies, so a slow earlier one cannot overwrite it. */
  async loadChat(id: string): Promise<void> {
    const ticket = ++this.loads
    this.view.loading(true)
    try {
      const loaded = await this.api.chat(id)
      if (ticket !== this.loads) return
      this.active = id
      this.view.chat(loaded.chat)
      this.showMessages(loaded.messages)
    } finally {
      if (ticket === this.loads) this.view.loading(false)
    }
  }

  async openChat(id: string): Promise<void> {
    await this.switchChats(async () => {
      await this.api.openChat(id)
      await this.loadChat(id)
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

  beginSend(text: string): void {
    this.setPending({ text, accepted: false, afterId: lastMessageId(this.shown), acceptedAt: 0 })
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
}
