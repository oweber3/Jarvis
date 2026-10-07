import { MicIcon, MoonIcon, PanelLeftIcon, SunIcon, XIcon } from "lucide-react"
import { useEffect, useState, type FC } from "react"
import { ModelPicker } from "@/components/ModelPicker"
import { ProjectsSidebar } from "@/components/ProjectsSidebar"
import { Thread } from "@/components/thread.aui"
import { TooltipProvider } from "@/components/ui/tooltip"
import { chatTitle } from "@/conversion"
import { chatValue, currentValue, describeValue } from "@/models"
import { applyTheme, nextTheme, readStoredTheme, storeTheme, type Theme } from "@/theme"
import { JarvisProvider, useJarvis } from "@/useJarvis"

export const App: FC = () => (
  <TooltipProvider>
    <JarvisProvider>
      <Shell />
    </JarvisProvider>
  </TooltipProvider>
)

const Shell: FC = () => {
  const { reachable, ready, toast, actions } = useJarvis()
  const [sidebarOpen, setSidebarOpen] = useState(true)

  return (
    <div className="bg-background text-foreground relative flex h-dvh w-full overflow-hidden">
      <aside
        className={`border-border bg-card/60 w-72 shrink-0 border-e max-md:absolute max-md:inset-y-0 max-md:z-30 max-md:shadow-xl ${sidebarOpen ? "" : "hidden"}`}
      >
        <ProjectsSidebar />
      </aside>
      <main className="flex min-w-0 flex-1 flex-col">
        <Header onToggleSidebar={() => setSidebarOpen((open) => !open)} />
        <StatusBanner reachable={reachable} ready={ready} />
        <ModelReminder />
        <div className="min-h-0 flex-1">
          <Thread components={{ ComposerLeading: ModelPicker }} />
        </div>
      </main>
      {toast && (
        <div role="alert" className="bg-popover text-popover-foreground border-border absolute bottom-24 left-1/2 z-40 flex max-w-md -translate-x-1/2 items-center gap-3 rounded-lg border px-4 py-2 text-sm shadow-lg">
          <span>{toast}</span>
          <button type="button" aria-label="Dismiss" onClick={actions.dismissToast} className="text-muted-foreground hover:text-foreground">
            <XIcon className="size-4" />
          </button>
        </div>
      )}
    </div>
  )
}

const Header: FC<{ onToggleSidebar: () => void }> = ({ onToggleSidebar }) => {
  const { chat } = useJarvis()
  return (
    <header className="border-border flex h-12 shrink-0 items-center gap-3 border-b px-3">
      <button
        type="button"
        onClick={onToggleSidebar}
        aria-label="Show or hide chats"
        className="text-muted-foreground hover:bg-muted hover:text-foreground flex size-8 items-center justify-center rounded-md"
      >
        <PanelLeftIcon className="size-4" />
      </button>
      <h1 className="min-w-0 flex-1 truncate text-sm font-medium">{chat ? chatTitle(chat) : "Jarvis"}</h1>
      <span
        className="text-muted-foreground flex shrink-0 items-center gap-1.5 text-xs"
        title="What you say to Jarvis by voice is added to this chat"
      >
        <MicIcon className="text-primary size-3.5" aria-hidden />
        Voice joins this chat
      </span>
      <ThemeSwitch />
    </header>
  )
}

// Light or dark, remembered in the browser. Dark is the Jarvis look.
const ThemeSwitch: FC = () => {
  const [theme, setTheme] = useState<Theme>(() => readStoredTheme())
  useEffect(() => {
    applyTheme(theme)
    storeTheme(theme)
  }, [theme])
  const light = theme === "light"
  return (
    <button
      type="button"
      role="switch"
      aria-checked={light}
      aria-label="Light mode"
      title={light ? "Switch to dark mode" : "Switch to light mode"}
      onClick={() => setTheme(nextTheme(theme))}
      className="text-muted-foreground hover:bg-muted hover:text-foreground flex size-8 shrink-0 items-center justify-center rounded-md"
    >
      {light ? <SunIcon className="size-4" /> : <MoonIcon className="size-4" />}
    </button>
  )
}

const StatusBanner: FC<{ reachable: boolean; ready: boolean }> = ({ reachable, ready }) => {
  if (reachable && ready) return null
  return (
    <div role="status" className="bg-warning/10 text-warning border-warning/30 border-b px-4 py-2 text-center text-sm">
      {reachable ? "Jarvis is starting. Messages can be sent once it is ready." : "Can't reach Jarvis. Is it running?"}
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
