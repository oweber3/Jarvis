import { useAui, useAuiState } from "@assistant-ui/react"
import type { FC } from "react"
import { usePhone } from "@/phone/PhoneProvider"

// One-tap commands from the PC's settings, each sent as if typed (so it shows in the chat and takes the same
// safety path).

export const QuickActions: FC = () => {
  const { phone } = usePhone()
  const aui = useAui()
  const unavailable = useAuiState((s) => s.thread.isDisabled || s.thread.isRunning)
  const actions = phone?.quick_actions ?? []
  if (actions.length === 0) return null
  return (
    <ul aria-label="Quick actions" className="no-scrollbar flex gap-2 overflow-x-auto px-3 pb-2">
      {actions.map((text) => (
        <li key={text} className="shrink-0">
          <button
            type="button"
            disabled={unavailable}
            onClick={() => void aui.thread().append({ role: "user", content: [{ type: "text", text }] })}
            className="border-border bg-muted/40 hover:bg-muted min-h-11 rounded-full border px-4 text-sm whitespace-nowrap transition-colors disabled:opacity-50"
          >
            {text}
          </button>
        </li>
      ))}
    </ul>
  )
}
