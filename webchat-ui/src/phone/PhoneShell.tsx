import { PanelLeftIcon, XIcon } from "lucide-react"
import { useEffect, useState, type FC } from "react"
import { ModelPicker } from "@/components/ModelPicker"
import { ProjectsSidebar } from "@/components/ProjectsSidebar"
import { StartScreen } from "@/components/StartScreen"
import { ThemeSwitch } from "@/components/ThemeSwitch"
import { TooltipProvider } from "@/components/ui/tooltip"
import { Thread } from "@/components/thread.aui"
import { chatTitle } from "@/conversion"
import { useNarrow } from "@/layout"
import { ConfirmCard } from "@/phone/ConfirmCard"
import { PhoneMicButton } from "@/phone/PhoneMicButton"
import { PhoneOrb } from "@/phone/PhoneOrb"
import { usePhone } from "@/phone/PhoneProvider"
import { PhoneSettings } from "@/phone/PhoneSettings"
import { QuickActions } from "@/phone/QuickActions"
import { StatusLine } from "@/phone/StatusBar"
import { useKeyboardOpen } from "@/phone/useKeyboardOpen"
import { useJarvis } from "@/useJarvis"

// The phone's page: the shared chat (thread, tool steps, projects, model picker, start screen) under the orb,
// with the phone-only pieces around it. The orb shrinks while the keyboard is up or a confirmation waits, so
// the conversation keeps its room.

const PhoneStart: FC = () => <StartScreen limit={4} />

const THREAD_COMPONENTS = {
  Welcome: PhoneStart,
  ComposerLeading: ModelPicker,
  ComposerTrailing: PhoneMicButton,
  // The PC's screen is not the phone's to attach.
  ComposerScreenshotButton: null,
}

export const PhoneShell: FC = () => {
  const { toast, chat, actions } = useJarvis()
  const { phone } = usePhone()
  const narrow = useNarrow()
  const keyboardOpen = useKeyboardOpen()
  const [sidebarOpen, setSidebarOpen] = useState(() => !narrow)
  const [seenNarrow, setSeenNarrow] = useState(narrow)
  if (seenNarrow !== narrow) {
    setSeenNarrow(narrow)
    setSidebarOpen(!narrow)
  }
  const overlay = narrow && sidebarOpen

  // An overlay closes once a chat has been chosen, and with Esc on a hardware keyboard.
  const openId = chat?.id
  useEffect(() => {
    if (narrow) setSidebarOpen(false)
  }, [openId, narrow])
  useEffect(() => {
    if (!overlay) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSidebarOpen(false)
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [overlay])

  const compact = keyboardOpen || !!(phone?.allow_confirm && phone.confirmation)

  return (
    <TooltipProvider>
      <div data-phone className="phone-root bg-background text-foreground relative flex w-full overflow-hidden">
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
          className={`border-border bg-card w-72 max-w-[85vw] shrink-0 border-e max-md:fixed max-md:inset-y-0 max-md:start-0 max-md:z-40 max-md:shadow-xl md:bg-card/60 ${sidebarOpen ? "" : "hidden"}`}
        >
          <div className="phone-sidebar h-full">
            <ProjectsSidebar canClearAll={false} />
          </div>
        </aside>
        <main className="flex min-w-0 flex-1 flex-col">
          <header className="border-border flex h-12 shrink-0 items-center gap-2 border-b px-3">
            <button
              type="button"
              onClick={() => setSidebarOpen((open) => !open)}
              aria-label="Show or hide chats"
              className="text-muted-foreground hover:bg-muted hover:text-foreground flex size-8 items-center justify-center rounded-md"
            >
              <PanelLeftIcon className="size-4" />
            </button>
            <h1 className="min-w-0 flex-1 truncate text-sm font-medium">{chat ? chatTitle(chat) : "Jarvis"}</h1>
            <PhoneSettings />
            <ThemeSwitch />
          </header>
          <PhoneOrb compact={compact} />
          <StatusLine />
          <ConfirmCard />
          {!keyboardOpen && <QuickActions />}
          <div className="min-h-0 flex-1">
            <Thread components={THREAD_COMPONENTS} autoFocus={false} />
          </div>
        </main>
        {toast && (
          <div
            role="alert"
            className="bg-popover text-popover-foreground border-border absolute bottom-28 left-1/2 z-40 flex w-[min(90vw,28rem)] -translate-x-1/2 items-center gap-3 rounded-lg border px-4 py-2 text-sm shadow-lg"
          >
            <span className="min-w-0 flex-1">{toast}</span>
            <button type="button" aria-label="Dismiss" onClick={actions.dismissToast} className="text-muted-foreground hover:text-foreground size-8">
              <XIcon className="mx-auto size-4" />
            </button>
          </div>
        )}
      </div>
    </TooltipProvider>
  )
}
