import { MenuIcon } from "lucide-react"
import { useEffect, useState, type FC } from "react"
import { StartScreen } from "@/components/StartScreen"
import { TooltipProvider } from "@/components/ui/tooltip"
import { Thread } from "@/components/thread.aui"
import { chatTitle } from "@/conversion"
import { ChatsDrawer } from "@/phone/ChatsDrawer"
import { ConnectionStatus } from "@/phone/ConnectionStatus"
import { PhoneComposerVoice } from "@/phone/PhoneMicButton"
import { PhoneDock } from "@/phone/PhoneDock"
import { PhoneSettings } from "@/phone/PhoneSettings"
import { QuickActionChips, QuickActionsButton } from "@/phone/QuickActions"
import { KeyboardOpen, useKeyboardOpen } from "@/phone/useKeyboardOpen"
import { VoiceModeDialog } from "@/phone/VoiceModeDialog"
import { useJarvis } from "@/useJarvis"

// The phone's page, chat first: a slim header (chats, title, connection dot, settings), the conversation in all
// the remaining height, and the message box pinned to the bottom above the keyboard and the safe area, with the
// dock (voice indicator, confirmation, toast) just above it. The orb lives in voice mode, a full-screen view
// opened from the button by the message box. The chats are a drawer; settings and quick actions are sheets.

const PhoneStart: FC = () => (
  <div className="my-auto flex flex-col gap-5 pb-4">
    <StartScreen limit={4} />
    <div className="px-2">
      <QuickActionChips />
    </div>
  </div>
)

const THREAD_COMPONENTS = {
  Welcome: PhoneStart,
  ComposerLeading: QuickActionsButton,
  ComposerTrailing: PhoneComposerVoice,
  ComposerAbove: PhoneDock,
  // The PC's screen is not the phone's to attach.
  ComposerScreenshotButton: null,
}

export const PhoneShell: FC = () => {
  const { chat } = useJarvis()
  const keyboardOpen = useKeyboardOpen()
  const [drawerOpen, setDrawerOpen] = useState(false)

  // The drawer closes once a chat has been chosen.
  const openId = chat?.id
  useEffect(() => setDrawerOpen(false), [openId])

  return (
    <KeyboardOpen.Provider value={keyboardOpen}>
      <TooltipProvider>
        <div
          data-phone
          data-keyboard={keyboardOpen ? "open" : "closed"}
          className="phone-root bg-background text-foreground relative flex w-full flex-col overflow-hidden"
        >
          <header className="border-border bg-background/95 flex h-14 shrink-0 items-center gap-1 border-b px-1.5">
            <button
              type="button"
              onClick={() => setDrawerOpen(true)}
              aria-label="Chats"
              aria-haspopup="dialog"
              aria-expanded={drawerOpen}
              className="text-muted-foreground hover:bg-muted hover:text-foreground flex size-11 shrink-0 items-center justify-center rounded-full"
            >
              <MenuIcon className="size-5" aria-hidden />
            </button>
            <h1 className="min-w-0 flex-1 truncate px-1 text-base font-semibold">{chat ? chatTitle(chat) : "Jarvis"}</h1>
            <ConnectionStatus />
            <PhoneSettings />
          </header>
          <main className="min-h-0 flex-1">
            <Thread components={THREAD_COMPONENTS} autoFocus={false} />
          </main>
        </div>
        <ChatsDrawer open={drawerOpen} onOpenChange={setDrawerOpen} />
        <VoiceModeDialog />
      </TooltipProvider>
    </KeyboardOpen.Provider>
  )
}
