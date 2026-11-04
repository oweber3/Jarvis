import { useAui, useAuiState } from "@assistant-ui/react"
import { KeyboardIcon, MicIcon, PanelLeftIcon, XIcon } from "lucide-react"
import { useEffect, useState, type FC } from "react"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { COMPOSER_KEYS, fieldOf, resolveShortcut, SHORTCUTS } from "@/shortcuts"
import { presence } from "@/presence"
import { useNarrow } from "@/layout"
import { MicButton } from "@/components/MicButton"
import { ModelPicker } from "@/components/ModelPicker"
import { ProjectsSidebar } from "@/components/ProjectsSidebar"
import { Thread } from "@/components/thread.aui"
import { StartScreen } from "@/components/StartScreen"
import { TooltipProvider } from "@/components/ui/tooltip"
import { chatTitle } from "@/conversion"
import { chatValue, currentValue, describeValue } from "@/models"
import { ErrorBoundary } from "@/components/ErrorBoundary"
import { ReplyAnnouncer } from "@/components/ReplyAnnouncer"
import { ThemeSwitch } from "@/components/ThemeSwitch"
import { JarvisProvider, useJarvis } from "@/useJarvis"

export const App: FC = () => (
  <ErrorBoundary>
    <TooltipProvider>
      <JarvisProvider>
        <Shell />
      </JarvisProvider>
    </TooltipProvider>
  </ErrorBoundary>
)

const TOAST_MS = 8000

const Shell: FC = () => {
  const { reachable, ready, toast, chat, actions } = useJarvis()
  const narrow = useNarrow()
  // Wide windows start with the chat list beside the chat; narrow ones start with it closed.
  const [sidebarOpen, setSidebarOpen] = useState(() => !narrow)
  const [seenNarrow, setSeenNarrow] = useState(narrow)
  if (seenNarrow !== narrow) {
    setSeenNarrow(narrow)
    setSidebarOpen(!narrow)
  }
  const overlay = narrow && sidebarOpen
  const [helpOpen, setHelpOpen] = useState(false)
  const running = useAuiState((s) => s.thread.isRunning)
  const aui = useAui()

  // Keyboard shortcuts (shortcuts.ts decides; this performs). Never a plain letter, so typing is safe.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const action = resolveShortcut(event, {
        running,
        overlayOpen: overlay,
        dialogOpen: document.querySelector("[role='dialog'], [role='menu'], [role='listbox']") !== null,
        field: fieldOf(document.activeElement),
        composerEmpty: aui.composer().getState().text.trim() === "",
      })
      if (!action) return
      if (action === "edit-last" && !editLastMessage()) return
      event.preventDefault()
      if (action === "toggle-chats") {
        setSidebarOpen((open) => !open)
      } else if (action === "focus-composer") {
        document.querySelector<HTMLTextAreaElement>('[aria-label="Message input"]')?.focus()
      } else if (action === "edit-last") {
        /* started above */
      } else if (action === "focus-search") {
        setSidebarOpen(true)
        window.requestAnimationFrame(() => {
          const box = document.querySelector<HTMLInputElement>("[data-chat-search]")
          box?.focus()
          box?.select()
        })
      } else if (action === "new-chat") {
        void actions.newChat(null)
      } else if (action === "stop") {
        aui.composer().cancel()
      } else {
        setHelpOpen(true)
      }
    }
    // The up arrow in an empty message box: the owner's last stored message opens for editing, while editing is
    // offered (not while the page shows an earlier stretch of the chat).
    const editLastMessage = (): boolean => {
      const thread = aui.thread().getState()
      if (!thread.capabilities.edit) return false
      const messages = thread.messages
      for (let i = messages.length - 1; i >= 0; i--) {
        const message = messages[i]!
        if (message.role === "user" && /^m\d+$/.test(message.id)) {
          aui.thread().message({ id: message.id }).composer().beginEdit()
          return true
        }
      }
      return false
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [actions, aui, overlay, running])

  // An overlay closes with Esc, and once a chat has been chosen.
  useEffect(() => {
    if (!overlay) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.defaultPrevented) setSidebarOpen(false)
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [overlay])
  const openId = chat?.id
  useEffect(() => {
    if (narrow) setSidebarOpen(false)
  }, [openId, narrow])

  // A message for the owner goes by itself after a while; Dismiss takes it at once.
  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(actions.dismissToast, TOAST_MS)
    return () => window.clearTimeout(timer)
  }, [toast, actions])

  return (
    <div className="bg-background text-foreground relative flex h-dvh w-full overflow-hidden">
      <a
        href="#message-box"
        onClick={(event) => {
          event.preventDefault()
          document.querySelector<HTMLTextAreaElement>('[aria-label="Message input"]')?.focus()
        }}
        className="bg-popover text-popover-foreground focus-visible:ring-ring sr-only z-50 rounded-md px-3 py-2 text-sm focus:not-sr-only focus:absolute focus:start-3 focus:top-3 focus-visible:ring-2"
      >
        Skip to the message box
      </a>
      <ReplyAnnouncer />
      {overlay && (
        <div
          data-testid="sidebar-backdrop"
          aria-hidden
          className="fixed inset-0 z-30 bg-black/50"
          onClick={() => setSidebarOpen(false)}
        />
      )}
      <aside
        aria-label="Chats"
        className={`border-border bg-card w-72 max-w-[85vw] shrink-0 border-e max-md:fixed max-md:inset-y-0 max-md:start-0 max-md:z-40 max-md:shadow-2xl md:bg-card/50 ${sidebarOpen ? "" : "hidden"}`}
      >
        <ProjectsSidebar />
      </aside>
      <main className="flex min-w-0 flex-1 flex-col">
        <Header onToggleSidebar={() => setSidebarOpen((open) => !open)} onShowShortcuts={() => setHelpOpen(true)} />
        <StatusBanner reachable={reachable} ready={ready} />
        <ModelReminder />
        <div className="min-h-0 flex-1">
          <Thread components={{ ComposerLeading: ModelPicker, Welcome: StartScreen, ComposerTrailing: MicButton }} />
        </div>
      </main>
      <ShortcutsDialog open={helpOpen} onOpenChange={setHelpOpen} />
      {toast && (
        <div role="alert" className="bg-popover text-popover-foreground border-border animate-in fade-in slide-in-from-bottom-2 absolute bottom-28 left-1/2 z-40 flex max-w-md -translate-x-1/2 items-center gap-3 rounded-xl border px-4 py-2.5 text-sm shadow-(--shadow-raised)">
          <span>{toast}</span>
          <button type="button" aria-label="Dismiss" onClick={actions.dismissToast} className="text-muted-foreground hover:text-foreground">
            <XIcon className="size-4" />
          </button>
        </div>
      )}
    </div>
  )
}

const Header: FC<{ onToggleSidebar: () => void; onShowShortcuts: () => void }> = ({ onToggleSidebar, onShowShortcuts }) => {
  const { chat } = useJarvis()
  return (
    <header className="border-border bg-background/80 supports-[backdrop-filter]:bg-background/60 flex h-14 shrink-0 items-center gap-2 border-b px-3 backdrop-blur-md">
      <button
        type="button"
        onClick={onToggleSidebar}
        aria-label="Show or hide chats"
        title="Show or hide chats (Ctrl+B)"
        className="text-muted-foreground hover:bg-muted hover:text-foreground flex size-9 shrink-0 items-center justify-center rounded-lg transition-colors"
      >
        <PanelLeftIcon className="size-4" />
      </button>
      <div className="flex min-w-0 items-center gap-2.5">
        <h1 className="min-w-0 truncate text-[15px] font-semibold tracking-tight">{chat ? chatTitle(chat) : "Jarvis"}</h1>
        <Presence />
      </div>
      <span className="flex-1" />
      <span
        className="text-muted-foreground sm:border-border flex shrink-0 items-center gap-1.5 rounded-full text-xs sm:border sm:px-2.5 sm:py-1"
        title="What you say to Jarvis by voice is added to this chat"
      >
        <MicIcon className="text-primary size-3.5" aria-hidden />
        <span className="max-sm:sr-only">Voice joins this chat</span>
      </span>
      <button
        type="button"
        onClick={onShowShortcuts}
        aria-label="Keyboard shortcuts"
        title="Keyboard shortcuts (?)"
        className="text-muted-foreground hover:bg-muted hover:text-foreground flex size-9 shrink-0 items-center justify-center rounded-lg transition-colors"
      >
        <KeyboardIcon className="size-4" />
      </button>
      <ThemeSwitch />
    </header>
  )
}

// Whether Jarvis can be reached and what it is doing, as the desktop chat's contact header says it. Read out
// politely when it changes.
const PRESENCE_DOT = {
  online: "bg-[var(--hud-success)]",
  working: "bg-[var(--hud-accent-indigo)] motion-safe:animate-pulse",
  waiting: "bg-warning",
  offline: "bg-destructive",
} as const

const Presence: FC = () => {
  const { reachable, ready, activity } = useJarvis()
  const now = presence({ reachable, ready, ...activity })
  return (
    <span
      role="status"
      data-slot="presence"
      data-tone={now.tone}
      className="text-muted-foreground bg-muted/50 flex shrink-0 items-center gap-1.5 rounded-full px-2 py-0.5 text-xs"
    >
      <span aria-hidden className={`size-1.5 rounded-full ${PRESENCE_DOT[now.tone]}`} />
      <span className="max-sm:sr-only">{now.label}</span>
    </span>
  )
}

// What the keyboard can do. Opened with the header button or "?".
const ShortcutsDialog: FC<{ open: boolean; onOpenChange: (open: boolean) => void }> = ({ open, onOpenChange }) => (
  <Dialog open={open} onOpenChange={onOpenChange}>
    <DialogContent className="sm:max-w-sm">
      <DialogHeader>
        <DialogTitle>Keyboard shortcuts</DialogTitle>
        <DialogDescription>On a Mac, use Cmd in place of Ctrl.</DialogDescription>
      </DialogHeader>
      <ul className="flex flex-col gap-2 text-sm">
        {[...SHORTCUTS, ...COMPOSER_KEYS].map((shortcut) => (
          <li key={shortcut.label} className="flex items-center justify-between gap-4">
            <span>{shortcut.label}</span>
            <span className="flex shrink-0 gap-1">
              {shortcut.keys.map((name) => (
                <kbd key={name} className="bg-muted text-muted-foreground rounded border px-1.5 py-0.5 font-mono text-xs">
                  {name}
                </kbd>
              ))}
            </span>
          </li>
        ))}
      </ul>
    </DialogContent>
  </Dialog>
)

const StatusBanner: FC<{ reachable: boolean; ready: boolean }> = ({ reachable, ready }) => {
  const { actions } = useJarvis()
  if (reachable && ready) return null
  return (
    <div
      role="status"
      className="bg-warning/10 text-warning border-warning/30 flex flex-wrap items-center justify-center gap-x-3 gap-y-1 border-b px-4 py-2 text-center text-sm"
    >
      <span>
        {reachable
          ? "Jarvis is starting. Messages can be sent once it is ready."
          : "Can't reach Jarvis. Is it running? The page keeps trying."}
      </span>
      {!reachable && (
        <button
          type="button"
          onClick={actions.retryNow}
          className="focus-visible:ring-ring/50 rounded-md px-2 py-0.5 font-medium underline underline-offset-2 outline-none focus-visible:ring-2"
        >
          Retry now
        </button>
      )}
    </div>
  )
}

// A chat remembers what it last used, but opening it never switches anything: one tap does.
const ModelReminder: FC = () => {
  const { chat, mode, modelState, models, actions } = useJarvis()
  if (!chat) return null
  const last = chatValue(chat.last_mode, chat.last_model)
  const now = currentValue(mode?.mode, modelState.current, null)
  if (!last || !now || last === now) return null
  const allowed = last.startsWith("mode:") ? mode?.enabled.includes(last.slice(5)) : modelState.switchable
  return (
    <div role="status" className="border-border bg-muted/40 flex items-center justify-center gap-3 border-b px-4 py-1.5 text-xs">
      <span>This chat last used {describeValue(last, models)}.</span>
      {allowed && (
        <button type="button" onClick={() => void actions.chooseModel(last)} className="text-primary hover:underline">
          Switch back
        </button>
      )}
    </div>
  )
}
