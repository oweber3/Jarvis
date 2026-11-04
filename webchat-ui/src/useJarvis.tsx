import {
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
  type AppendMessage,
  type ThreadMessageLike,
} from "@assistant-ui/react"
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import {
  ApiError,
  api,
  describeError,
  type AttachmentSupport,
  type Chat,
  type ChatMessage,
  type CloudModel,
  type CloudState,
  type Library,
  type LocalModel,
  type LocalModelState,
  type Notice,
  type ReplyModes,
  type Snapshot,
} from "@/api"
import { JarvisAttachmentAdapter } from "@/attachmentAdapter"
import { buildThreadMessages, chatTitle, convertThreadMessage, type Pending } from "@/conversion"
import { ChatSync, type HistoryWindow } from "@/sync"

// All of the page's state. The server owns the conversation (voice and typed turns land in the open chat
// the same way), so this follows it with a long poll and sends only what the owner does.

export type JarvisActions = {
  newChat: (projectId?: string | null) => Promise<void>
  /** Opens a chat on its newest messages, or on the stretch around ``messageId`` (a search result). */
  openChat: (id: string, messageId?: number) => Promise<void>
  /** Back to the newest messages after scrolling far back in a long chat. */
  jumpToLatest: () => Promise<void>
  moveChat: (id: string, projectId: string | null) => Promise<void>
  pinChat: (id: string, pinned: boolean) => Promise<void>
  createProject: (name: string) => Promise<void>
  renameProject: (id: string, name: string) => Promise<void>
  deleteProject: (id: string) => Promise<void>
  /** A picker option: "mode:<name>", "local:<model>" or "cloud:<model>" (a model of the active cloud mode). */
  chooseModel: (value: string) => Promise<void>
  /** A model and effort of the active Claude or Codex mode; an omitted effort keeps the current one when offered. */
  chooseCloudModel: (model: string, effort?: string) => Promise<void>
  clearHistory: () => Promise<void>
  dismissToast: () => void
  /**
   * A request made outside the composer (the phone's spoken one): show it at once as a thinking bubble,
   * then say whether the PC took it. The reply arrives through the poll like any other.
   */
  beginSend: (text: string) => void
  sendAccepted: () => void
  sendFailed: () => void
  showToast: (message: string) => void
}

/** What the phone app adds to the page's behaviour; the web chat passes none. */
export type JarvisHooks = {
  /** Every poll snapshot as it arrives. */
  onSnapshot?: (snapshot: Snapshot) => void
  /** Whether a typed reply is to be spoken to this device. */
  speakReplies?: () => boolean
  /** A typed request was taken, with the query id the PC gave it and whether its reply is to be spoken. */
  onSent?: (queryId: number, speak: boolean) => void
}

export type JarvisView = {
  ready: boolean
  reachable: boolean
  library: Library
  chat: Chat | null
  /** How much of the open chat the page holds (a long chat is loaded a page at a time). */
  history: HistoryWindow
  mode: ReplyModes | null
  modelState: LocalModelState
  models: LocalModel[]
  cloud: CloudState | null
  cloudModels: CloudModel[]
  /** What attaching can do for the route that answers now; null until the server has said. */
  attachmentSupport: AttachmentSupport | null
  toast: string | null
  actions: JarvisActions
}

const EMPTY_LIBRARY: Library = { projects: [], chats: [], active_chat_id: null }
const NO_NOTICES: Notice[] = []
const NO_MODEL: LocalModelState = { current: null, switchable: false }

const JarvisContext = createContext<JarvisView | null>(null)

export function useJarvis(): JarvisView {
  const view = useContext(JarvisContext)
  if (!view) throw new Error("useJarvis must be used inside JarvisProvider")
  return view
}

// The stored id behind a thread message id ("m<id>"), or null for a notice, a pending message or nothing.
function storedMessageId(threadId: string | null | undefined): number | null {
  const found = threadId ? /^m(\d+)$/.exec(threadId) : null
  return found ? Number(found[1]) : null
}

function textOf(message: AppendMessage): string {
  return message.content
    .map((part) => (part.type === "text" ? part.text : ""))
    .join("")
    .trim()
}

// A busy or unready Jarvis arrives as a notice from the server, so it is not also told as an error.
const toldByNotice = (error: unknown) => error instanceof ApiError && ["busy", "unavailable"].includes(error.code)

export function JarvisProvider({ children, hooks }: { children: ReactNode; hooks?: JarvisHooks }) {
  const hooksRef = useRef<JarvisHooks | undefined>(hooks)
  hooksRef.current = hooks
  const [library, setLibrary] = useState<Library>(EMPTY_LIBRARY)
  const [chat, setChat] = useState<Chat | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [notices, setNotices] = useState<Notice[]>([])
  const [pending, setPending] = useState<Pending | null>(null)
  const [meta, setMeta] = useState<Pick<Snapshot, "ready" | "busy" | "busy_query" | "mode" | "model" | "cloud">>({
    ready: false,
    busy: false,
    busy_query: false,
    mode: null,
    model: NO_MODEL,
    cloud: null,
  })
  const [models, setModels] = useState<LocalModel[]>([])
  const [cloudModels, setCloudModels] = useState<CloudModel[]>([])
  const [attachmentSupport, setAttachmentSupport] = useState<AttachmentSupport | null>(null)
  const [reachable, setReachable] = useState(true)
  const [loadingChat, setLoadingChat] = useState(true)
  const [history, setHistory] = useState<HistoryWindow>({ hasEarlier: false, hasLater: false })
  const [stopping, setStopping] = useState(false)
  const [toast, setToast] = useState<string | null>(null)

  const chatRef = useRef<Chat | null>(null)
  chatRef.current = chat

  // The page follows the server with a long poll and applies the owner's own actions on top (sync.ts).
  const [sync] = useState(
    () =>
      new ChatSync(api, {
        snapshot: (snapshot) => hooksRef.current?.onSnapshot?.(snapshot),
        library: setLibrary,
        chat: setChat,
        messages: setMessages,
        notices: setNotices,
        meta: setMeta,
        loading: setLoadingChat,
        pending: setPending,
        reachable: setReachable,
        window: setHistory,
        stopping: setStopping,
      }),
  )

  const refreshLibrary = useCallback(() => sync.refreshLibrary(), [sync])

  const refreshModels = useCallback(async () => {
    try {
      const listed = await api.models()
      setModels(listed.models)
      setCloudModels(listed.cloud?.models ?? [])
    } catch {
      /* the picker keeps what it had */
    }
  }, [])

  const fail = useCallback((error: unknown) => setToast(describeError(error)), [])

  // What the owner attaches is held by the server until the message goes; the adapter keeps the page's chips
  // and the server's ids apart. It reads the limits the server last reported.
  const supportRef = useRef<AttachmentSupport | null>(null)
  supportRef.current = attachmentSupport
  const [attachmentAdapter] = useState(
    () =>
      new JarvisAttachmentAdapter(
        { upload: api.uploadAttachment, screenshot: api.captureScreenshot, discard: api.discardAttachment },
        () => supportRef.current?.limits ?? null,
        setToast,
      ),
  )

  const refreshAttachmentSupport = useCallback(async () => {
    try {
      setAttachmentSupport(await api.attachmentSupport())
    } catch {
      /* the composer keeps what it knew; the server still refuses what cannot go */
    }
  }, [])

  useEffect(() => {
    const abort = new AbortController()
    void sync.run(abort.signal)
    return () => abort.abort()
  }, [sync])

  const mode = meta.mode?.mode
  const current = meta.model.current
  const cloudKey = meta.cloud ? `${meta.cloud.mode}|${meta.cloud.model}|${meta.cloud.effort}|${meta.cloud.ready}` : ""
  useEffect(() => {
    void refreshModels()
    void refreshAttachmentSupport()
  }, [mode, current, cloudKey, refreshModels, refreshAttachmentSupport])

  const actions = useMemo<JarvisActions>(() => {
    const guarded = (work: () => Promise<unknown>) => async () => {
      try {
        await work()
      } catch (error) {
        fail(error)
      }
    }
    return {
      async newChat(projectId = null) {
        const open = chatRef.current
        if (open && !open.title && sync.messages.length === 0 && sync.pending === null) {
          // Already in an empty chat: use it, in the project asked for, instead of piling up empty ones.
          if (open.project_id !== projectId) {
            await guarded(async () => {
              await api.moveChat(open.id, projectId)
              await refreshLibrary()
            })()
          }
          return
        }
        await guarded(() => sync.newChat(projectId))()
      },
      async openChat(id, messageId) {
        if (messageId !== undefined && id === sync.activeId && sync.messages.some((m) => m.id === messageId)) return
        await guarded(() => sync.openChat(id, messageId))()
      },
      async jumpToLatest() {
        await guarded(() => sync.jumpToLatest())()
      },
      async moveChat(id, projectId) {
        await guarded(async () => {
          await api.moveChat(id, projectId)
          await refreshLibrary()
        })()
      },
      async pinChat(id, pinned) {
        await guarded(async () => {
          await api.pinChat(id, pinned)
          await refreshLibrary()
        })()
      },
      async createProject(name) {
        await guarded(async () => {
          await api.createProject(name)
          await refreshLibrary()
        })()
      },
      async renameProject(id, name) {
        await guarded(async () => {
          await api.renameProject(id, name)
          await refreshLibrary()
        })()
      },
      async deleteProject(id) {
        await guarded(async () => {
          await api.deleteProject(id)
          await refreshLibrary()
        })()
      },
      async chooseModel(value) {
        const [kind, ...rest] = value.split(":")
        const target = rest.join(":")
        await guarded(async () => {
          if (kind === "mode") await api.setModel("mode", target)
          else if (kind === "cloud") await api.setCloudModel(target)
          else if (kind === "local") {
            if (meta.mode?.mode !== "local") await api.setModel("mode", "local")
            if (meta.model.current !== target) await api.setModel("local", target)
          }
        })()
        await refreshModels()
      },
      async chooseCloudModel(model, effort) {
        await guarded(() => api.setCloudModel(model, effort))()
        await refreshModels()
      },
      async clearHistory() {
        await guarded(async () => {
          await api.clear()
          sync.clearPending()
          await refreshLibrary()
        })()
      },
      dismissToast: () => setToast(null),
      beginSend: (text) => sync.beginSend(text),
      sendAccepted: () => sync.sendAccepted(),
      sendFailed: () => sync.sendFailed(),
      showToast: setToast,
    }
  }, [fail, meta.mode, meta.model.current, refreshLibrary, refreshModels, sync])

  const sendNew = async (message: AppendMessage) => {
    const text = textOf(message)
    // A message goes below the newest turns, so a window that let go of them comes back first.
    if (sync.detached) await sync.jumpToLatest()
    const attached = attachmentAdapter.take((message.attachments ?? []).map((a) => a.id))
    const forget = () => attached.forEach((a) => void api.discardAttachment(a.serverId).catch(() => undefined))
    if (!text) return forget()
    sync.beginSend(text, attached.map(({ kind, size }) => ({ kind, size })))
    try {
      const speak = hooksRef.current?.speakReplies?.() ?? false
      const sent = await api.send(text, attached.map((a) => a.serverId), speak)
      sync.sendAccepted()
      hooksRef.current?.onSent?.(sent.query_id, speak)
    } catch (error) {
      sync.sendFailed()
      forget()
      if (toldByNotice(error)) giveBack(text, attached.length > 0)
      else fail(error)
    }
  }

  // A message Jarvis could not take (busy, not ready) goes back into the message box, unless the owner has started
  // another one; its attachments were used up by the attempt.
  const giveBack = (text: string, hadAttachments: boolean) => {
    const composer = runtimeRef.current?.thread.composer
    if (!composer || composer.getState().text.trim()) return
    composer.setText(text)
    if (hadAttachments) setToast("The message wasn't sent, so its words are back in the message box. Attach the files again.")
  }

  // ``text`` is the new wording (undefined asks the stored words again); ``shown`` is what the thread shows meanwhile.
  const askAgain = async (messageId: number, text: string | undefined, shown: string) => {
    const chatId = sync.activeId
    if (chatId === null) return
    sync.beginRewind(messageId, shown)
    try {
      const speak = hooksRef.current?.speakReplies?.() ?? false
      const sent = await api.rewind(chatId, messageId, text, speak)
      sync.sendAccepted()
      hooksRef.current?.onSent?.(sent.query_id, speak)
    } catch (error) {
      await sync.rewindFailed().catch(() => undefined)
      fail(error)
    }
  }

  // Notices belong to the newest moment of the chat, so a window that stops short of it shows none.
  const shownNotices = history.hasLater ? NO_NOTICES : notices
  const threadMessages = useMemo(
    () => buildThreadMessages(messages, shownNotices, pending),
    [messages, shownNotices, pending],
  )

  const runtimeRef = useRef<ReturnType<typeof useExternalStoreRuntime> | null>(null)
  const runtime = useExternalStoreRuntime<ThreadMessageLike>({
    messages: threadMessages,
    convertMessage: convertThreadMessage,
    // Stop gives the message box back at once, as the desktop chat does; the server's notice follows.
    isRunning: !stopping && ((pending !== null && !pending.stopped) || meta.busy_query),
    isLoading: loadingChat,
    isDisabled: !meta.ready || !reachable,
    hasEarlier: history.hasEarlier,
    onLoadEarlier: () => sync.loadEarlier(),
    onNew: (message) => sendNew(message),
    // Edit: a stored message is asked again with the new words (it and what followed are replaced). A message that
    // was never stored (it failed or was stopped) is simply sent again as edited.
    onEdit: async (message) => {
      const stored = storedMessageId(message.sourceId)
      if (stored === null) return sendNew(message)
      const text = textOf(message)
      if (text) await askAgain(stored, text, text)
    },
    // Regenerate: the owner's message before a reply is asked again as it was.
    onReload: async (parentId) => {
      const stored = storedMessageId(parentId)
      const original = sync.messages.find((m) => m.id === stored)
      if (stored !== null && original?.role === "user") await askAgain(stored, undefined, original.text)
    },
    onCancel: async () => {
      sync.stopRequested()
      try {
        await api.stop()
      } catch (error) {
        fail(error)
      }
    },
    adapters: {
      attachments: attachmentAdapter,
      threadList: {
        threadId: library.active_chat_id ?? chat?.id ?? "",
        threads: library.chats.map((c) => ({ id: c.id, status: "regular" as const, title: chatTitle(c) })),
        onSwitchToNewThread: () => actions.newChat(null),
        onSwitchToThread: (id) => actions.openChat(id),
        onRename: async (id, title) => {
          try {
            await api.renameChat(id, title)
            await refreshLibrary()
          } catch (error) {
            fail(error)
          }
        },
        onDelete: async (id) => {
          try {
            await api.deleteChat(id)
            await refreshLibrary()
          } catch (error) {
            fail(error)
          }
        },
      },
    },
  })

  runtimeRef.current = runtime

  const view = useMemo<JarvisView>(
    () => ({
      ready: meta.ready,
      reachable,
      library,
      chat,
      history,
      mode: meta.mode,
      modelState: meta.model,
      models,
      cloud: meta.cloud,
      cloudModels,
      attachmentSupport,
      toast,
      actions,
    }),
    [
      actions,
      attachmentSupport,
      chat,
      cloudModels,
      history,
      library,
      meta.cloud,
      meta.mode,
      meta.model,
      meta.ready,
      models,
      reachable,
      toast,
    ],
  )

  return (
    <JarvisContext.Provider value={view}>
      <AssistantRuntimeProvider runtime={runtime}>{children}</AssistantRuntimeProvider>
    </JarvisContext.Provider>
  )
}
