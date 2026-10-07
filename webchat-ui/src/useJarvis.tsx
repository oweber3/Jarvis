import { useExternalStoreRuntime, type AppendMessage, type ThreadMessageLike } from "@assistant-ui/react"
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import {
  api,
  describeError,
  type Chat,
  type ChatMessage,
  type Library,
  type LocalModel,
  type LocalModelState,
  type Project,
  type ReplyModes,
  type Snapshot,
  type Notice,
} from "@/api"
import { buildThreadMessages, chatTitle, lastMessageId, mergeMessages, type Pending } from "@/conversion"

// All of the page's state. The server owns the conversation (voice and typed turns land in the open chat
// the same way), so this follows it with a long poll and sends only what the owner does.

export type JarvisActions = {
  newChat: (projectId?: string | null) => Promise<void>
  openChat: (id: string) => Promise<void>
  moveChat: (id: string, projectId: string | null) => Promise<void>
  createProject: (name: string) => Promise<void>
  renameProject: (id: string, name: string) => Promise<void>
  deleteProject: (id: string) => Promise<void>
  chooseModel: (value: string) => Promise<void>
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

export function JarvisProvider({ children }: { children: ReactNode }) {
  const [library, setLibrary] = useState<Library>(EMPTY_LIBRARY)
  const [chat, setChat] = useState<Chat | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [notices, setNotices] = useState<Notice[]>([])
  const [pending, setPending] = useState<Pending | null>(null)
  const [meta, setMeta] = useState<Pick<Snapshot, "ready" | "busy" | "busy_query" | "mode" | "model">>({
    ready: false,
    busy: false,
    busy_query: false,
    mode: null,
    model: NO_MODEL,
  })
  const [models, setModels] = useState<LocalModel[]>([])
  const [reachable, setReachable] = useState(true)
  const [loadingChat, setLoadingChat] = useState(true)
  const [toast, setToast] = useState<string | null>(null)

  const afterRef = useRef(0)
  const activeRef = useRef<string | null>(null)
  const libraryRevRef = useRef(-1)
  const pendingRef = useRef<Pending | null>(null)
  pendingRef.current = pending

  const refreshLibrary = useCallback(async () => {
    setLibrary(await api.library())
  }, [])

  const refreshModels = useCallback(async () => {
    try {
      setModels((await api.models()).models)
    } catch {
      /* the selector keeps what it had */
    }
  }, [])

  const loadChat = useCallback(async (id: string) => {
    setLoadingChat(true)
    try {
      const loaded = await api.chat(id)
      activeRef.current = id
      setChat(loaded.chat)
      setMessages(loaded.messages)
      setNotices([])
      afterRef.current = lastMessageId(loaded.messages)
    } finally {
      setLoadingChat(false)
    }
  }, [])

  const fail = useCallback((error: unknown) => setToast(describeError(error)), [])

  // The long poll: one request waits on the server until something changes.
  useEffect(() => {
    const abort = new AbortController()
    let rev = -1
    let backoff = 1000

    const apply = async (snap: Snapshot) => {
      rev = snap.rev
      setMeta({ ready: snap.ready, busy: snap.busy, busy_query: snap.busy_query, mode: snap.mode, model: snap.model })
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
          setMessages([])
          afterRef.current = 0
          setLoadingChat(false)
        }
      } else {
        if (snap.chat) setChat(snap.chat)
        setMessages((current) => {
          const merged = mergeMessages(current, snap.messages)
          afterRef.current = lastMessageId(merged)
          return merged
        })
      }
      // The reply is stored before the query reads done, so the thinking placeholder can go.
      if (!snap.busy_query && pendingRef.current?.accepted) setPending(null)
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
  }, [loadChat, refreshLibrary])

  const mode = meta.mode?.mode
  const current = meta.model.current
  useEffect(() => {
    void refreshModels()
  }, [mode, current, refreshModels])

  const actions = useMemo<JarvisActions>(
    () => ({
      async newChat(projectId = null) {
        try {
          const created = await api.createChat(projectId)
          await Promise.all([loadChat(created.chat.id), refreshLibrary()])
        } catch (error) {
          fail(error)
        }
      },
      async openChat(id) {
        try {
          await api.openChat(id)
          await loadChat(id)
        } catch (error) {
          fail(error)
        }
      },
      async moveChat(id, projectId) {
        try {
          await api.moveChat(id, projectId)
          await refreshLibrary()
        } catch (error) {
          fail(error)
        }
      },
      async createProject(name) {
        try {
          await api.createProject(name)
          await refreshLibrary()
        } catch (error) {
          fail(error)
        }
      },
      async renameProject(id, name) {
        try {
          await api.renameProject(id, name)
          await refreshLibrary()
        } catch (error) {
          fail(error)
        }
      },
      async deleteProject(id) {
        try {
          await api.deleteProject(id)
          await refreshLibrary()
        } catch (error) {
          fail(error)
        }
      },
      async chooseModel(value) {
        // Options are "mode:<name>" for a reply mode and "local:<model>" for a local model on this PC.
        const [kind, ...rest] = value.split(":")
        const target = rest.join(":")
        try {
          if (kind === "mode") await api.setModel("mode", target)
          else if (kind === "local") {
            if (meta.mode?.mode !== "local") await api.setModel("mode", "local")
            if (meta.model.current !== target) await api.setModel("local", target)
          }
        } catch (error) {
          fail(error)
        }
        await refreshModels()
      },
      async clearHistory() {
        try {
          await api.clear()
          setPending(null)
          await refreshLibrary()
        } catch (error) {
          fail(error)
        }
      },
      dismissToast: () => setToast(null),
    }),
    [fail, loadChat, meta.mode, meta.model.current, refreshLibrary, refreshModels],
  )

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
      setPending({ text, accepted: false })
      try {
        await api.send(text)
        setPending({ text, accepted: true })
      } catch (error) {
        setPending(null)
        // A busy or unready Jarvis arrives as a notice from the server; anything else is told here.
        if (!(error instanceof Error && ["busy", "unavailable"].includes(error.message))) fail(error)
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
      toast,
      actions,
    }),
    [actions, chat, library, meta.mode, meta.model, meta.ready, models, reachable, toast],
  )

  return (
    <JarvisContext.Provider value={view}>
      <RuntimeBridge runtime={runtime}>{children}</RuntimeBridge>
    </JarvisContext.Provider>
  )
}

import { AssistantRuntimeProvider, type AssistantRuntime } from "@assistant-ui/react"

function RuntimeBridge({ runtime, children }: { runtime: AssistantRuntime; children: ReactNode }) {
  return <AssistantRuntimeProvider runtime={runtime}>{children}</AssistantRuntimeProvider>
}

export type { Project }
