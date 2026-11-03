import { useAui, useAuiState } from "@assistant-ui/react"
import { ZapIcon } from "lucide-react"
import { useState, type FC } from "react"
import { usePhone } from "@/phone/PhoneProvider"
import { Sheet } from "@/phone/Sheet"

// One-tap commands from the PC's settings (`remote_access_quick_actions`), each sent as if typed, so it shows in
// the chat and takes the same safety path. They show as chips on an empty chat, and in a bottom sheet behind
// the lightning button by the message box.

function useQuickActions() {
  const { phone } = usePhone()
  const aui = useAui()
  const unavailable = useAuiState((s) => s.thread.isDisabled || s.thread.isRunning)
  const send = (text: string) => void aui.thread().append({ role: "user", content: [{ type: "text", text }] })
  return { actions: phone?.quick_actions ?? [], unavailable, send }
}

export const QuickActionChips: FC = () => {
  const { actions, unavailable, send } = useQuickActions()
  if (actions.length === 0) return null
  return (
    <section aria-label="Quick actions" className="flex flex-col gap-2">
      <h2 className="text-muted-foreground flex items-center gap-1.5 text-xs font-medium">
        <ZapIcon aria-hidden className="size-3.5" />
        Quick actions
      </h2>
      <ul className="flex flex-wrap gap-2">
        {actions.map((text) => (
          <li key={text}>
            <button
              type="button"
              disabled={unavailable}
              onClick={() => send(text)}
              className="border-border bg-muted/40 hover:bg-muted min-h-11 rounded-full border px-4 text-sm transition-colors disabled:opacity-50"
            >
              {text}
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}

export const QuickActionsButton: FC = () => {
  const { actions, unavailable, send } = useQuickActions()
  const [open, setOpen] = useState(false)
  if (actions.length === 0) return null
  return (
    <>
      <button
        type="button"
        aria-label="Quick actions"
        aria-haspopup="dialog"
        onClick={() => setOpen(true)}
        className="text-muted-foreground hover:bg-muted hover:text-foreground flex size-11 items-center justify-center rounded-full"
      >
        <ZapIcon className="size-5" aria-hidden />
      </button>
      <Sheet open={open} onOpenChange={setOpen} title="Quick actions">
        <ul className="flex flex-col gap-2 pb-2">
          {actions.map((text) => (
            <li key={text}>
              <button
                type="button"
                disabled={unavailable}
                onClick={() => {
                  send(text)
                  setOpen(false)
                }}
                className="border-border bg-muted/40 hover:bg-muted flex min-h-12 w-full items-center gap-3 rounded-xl border px-4 text-start text-sm transition-colors disabled:opacity-50"
              >
                <ZapIcon aria-hidden className="text-primary size-4 shrink-0" />
                <span className="min-w-0 break-words">{text}</span>
              </button>
            </li>
          ))}
        </ul>
      </Sheet>
    </>
  )
}
