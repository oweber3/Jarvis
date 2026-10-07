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
import { buildThreadMessages, chatTitle, lastMessageId, mergeMessages, pendingDone, type Pending } from "@/conversion"

// All of the page's state. The server owns the conversation (voice and typed turns land in the open chat
// the same way), so this follows it with a long poll and sends only what the owner does.

export type JarvisActions = {
  newChat: (projectId?: string | null) => Promise<void>
  openChat: (id: string) => Promise<void>
  moveChat: (id: string, projectId: string | null) => Promise<void>
  createProject: (name: string) => Promise<void>
  renameProject: (id: string, name: string) => Promise<void>
  deleteProject: (id: string) => Promise<void>
  /** A picker option: "mode:<name>", "local:<model>" or "cloud:<model>" (a model of the active cloud mode). */
  chooseModel: (value: string) => Promise<void>
  /** A model and effort of the active Claude or Codex mode; an omitted effort keeps the current one when offered. */
  chooseCloudModel: (model: string, effort?: string) => Promise<void>
  clearHistory: () => Promise<void>
  dismissToast: () => void
}

export type JarvisView = {
  ready: boolean
  reachable: boolean
  library: Library
  chat: Chat | null
  mode: ReplyModes | null
  modelState: LocalModelState
  models: LocalModel[]
  cloud: CloudState | null
  cloudModels: CloudModel[]
  toast: string | null
  actions: JarvisActions
}

const EMPTY_LIBRARY: Library = { projects: [], chats: [], active_chat_id: null }
const NO_MODEL: LocalModelState = { current: null, switchable: false }

const JarvisContext = createContext<JarvisView | null>(null)

export function useJarvis(): JarvisView {
  const view = useContext(JarvisContext)
  if (!view) throw new Error("useJarvis must be used inside JarvisProvider")
  return view
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

function textOf(message: AppendMessage): string {
  return message.content
    .map((part) => (part.type === "text" ? part.text : ""))
    .join("")
    .trim()
}

// A busy or unready Jarvis arrives as a notice from the server, so it is not also told as an error.
const toldByNotice = (error: unknown) => error instanceof ApiError && ["busy", "unavailable"].includes(error.code)

export function JarvisProvider({ children }: { children: ReactNode }) {
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
  const [reachable, setReachable] = useState(true)
  const [loadingChat, setLoadingChat] = useState(true)
  const [toast, setToast] = useState<string | null>(null)

  const afterRef = useRef(0)
  const activeRef = useRef<string | null>(null)
  const libraryRevRef = useRef(-1)
  const messagesRef = useRef<ChatMessage[]>([])
  const pendingRef = useRef<Pending | null>(null)
  pendingRef.current = pending
  const chatRef = useRef<Chat | null>(null)
  chatRef.current = chat

  const showMessages = useCallback((next: ChatMessage[]) => {
    messagesRef.current = next
    afterRef.current = lastMessageId(next)
    setMessages(next)
  }, [])

  const refreshLibrary = useCallback(async () => {
    setLibrary(await api.library())
  }, [])

  const refreshModels = useCallback(async () => {
    try {
      const listed = await api.models()
      setModels(listed.models)
      setCloudModels(listed.cloud?.models ?? [])
    } catch {
      /* the picker keeps what it had */
    }
  }, [])

  const loadChat = useCallback(
    async (id: string) => {
      setLoadingChat(true)
      try {
        const loaded = await api.chat(id)
        activeRef.current = id
        setChat(loaded.chat)
        setNotices([])
        showMessages(loaded.messages)
      } finally {
        setLoadingChat(false)
      }
    },
    [showMessages],
  )

  const fail = useCallback((error: unknown) => setToast(describeError(error)), [])

  // The long poll: one request waits on the server until something changes.
  useEffect(() => {
    const abort = new AbortController()
    let rev = -1
    let backoff = 1000

    const apply = async (snap: Snapshot) => {
      rev = snap.rev
      setMeta({
        ready: snap.ready,
        busy: snap.busy,
        busy_query: snap.busy_query,
        mode: snap.mode,
        model: snap.model,
        cloud: snap.cloud,
      })
      setNotices(snap.notices)
      if (snap.library_rev !== libraryRevRef.current) {
        libraryRevRef.current = snap.library_rev
        await refreshLibrary()
      }
      if (snap.active_chat_id !== activeRef.current) {
        if (snap.active_chat_id) await loadChat(snap.active_chat_id)
        else {
          activeRef.current = null
          setChat(null)
          showMessages([])
          setLoadingChat(false)
        }
      } else {
        if (snap.chat) setChat(snap.chat)
        showMessages(mergeMessages(messagesRef.current, snap.messages))
      }
      // The reply is stored before the query reads done, so the thinking placeholder can go.
      if (pendingDone(pendingRef.current, snap.busy_query, messagesRef.current)) setPending(null)
    }

    ;(async () => {
      while (!abort.signal.aborted) {
        try {
          await apply(await api.poll(rev, afterRef.current, abort.signal))
          setReachable(true)
          backoff = 1000
        } catch {
          if (abort.signal.aborted) return
          setReachable(false)
          await sleep(backoff)
          backoff = Math.min(backoff * 2, 15000)
        }
      }
    })()
    return () => abort.abort()
  }, [loadChat, refreshLibrary, showMessages])

  const mode = meta.mode?.mode
  const current = meta.model.current
  const cloudKey = meta.cloud ? `${meta.cloud.mode}|${meta.cloud.model}|${meta.cloud.effort}|${meta.cloud.ready}` : ""
  useEffect(() => {
    void refreshModels()
  }, [mode, current, cloudKey, refreshModels])

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
        if (open && !open.title && messagesRef.current.length === 0 && pendingRef.current === null) {
          // Already in an empty chat: use it, in the project asked for, instead of piling up empty ones.
          if (open.project_id !== projectId) {
            await guarded(async () => {
              await api.moveChat(open.id, projectId)
              await refreshLibrary()
            })()
          }
          return
        }
        await guarded(async () => {
          const created = await api.createChat(projectId)
          await Promise.all([loadChat(created.chat.id), refreshLibrary()])
        })()
      },
      async openChat(id) {
        await guarded(async () => {
          await api.openChat(id)
          await loadChat(id)
        })()
      },
      async moveChat(id, projectId) {
        await guarded(async () => {
          await api.moveChat(id, projectId)
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
          setPending(null)
          await refreshLibrary()
        })()
      },
      dismissToast: () => setToast(null),
    }
  }, [fail, loadChat, meta.mode, meta.model.current, refreshLibrary, refreshModels])

  const threadMessages = useMemo(() => buildThreadMessages(messages, notices, pending), [messages, notices, pending])

  const runtime = useExternalStoreRuntime<ThreadMessageLike>({
    messages: threadMessages,
    convertMessage: (message) => message,
    isRunning: pending !== null || meta.busy_query,
    isLoading: loadingChat,
    isDisabled: !meta.ready || !reachable,
    onNew: async (message) => {
      const text = textOf(message)
      if (!text) return
      setPending({ text, accepted: false, afterId: lastMessageId(messagesRef.current) })
      try {
        await api.send(text)
        setPending((current) => (current ? { ...current, accepted: true } : current))
      } catch (error) {
        setPending(null)
        if (!toldByNotice(error)) fail(error)
      }
    },
    onCancel: async () => {
      try {
        await api.stop()
      } catch (error) {
        fail(error)
      }
    },
    adapters: {
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

  const view = useMemo<JarvisView>(
    () => ({
      ready: meta.ready,
      reachable,
      library,
      chat,
      mode: meta.mode,
      modelState: meta.model,
      models,
      cloud: meta.cloud,
      cloudModels,
      toast,
      actions,
    }),
    [actions, chat, cloudModels, library, meta.cloud, meta.mode, meta.model, meta.ready, models, reachable, toast],
  )

  return (
    <JarvisContext.Provider value={view}>
      <AssistantRuntimeProvider runtime={runtime}>{children}</AssistantRuntimeProvider>
    </JarvisContext.Provider>
  )
}
