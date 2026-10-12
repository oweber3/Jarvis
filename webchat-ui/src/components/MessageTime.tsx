import type { FC } from "react"
import { useAuiState } from "@assistant-ui/react"
import { fullDateTime, messageTime } from "@/time"

// A message's time in the browser's own format, with the full date on hover.
export const MessageTime: FC<{ className?: string }> = ({ className }) => {
  const createdAt = useAuiState((s) => s.message.createdAt)
  if (!createdAt) return null
  const ts = createdAt.getTime() / 1000
  return (
    <time
      data-slot="aui_message-time"
      dateTime={createdAt.toISOString()}
      title={fullDateTime(ts)}
      className={`text-muted-foreground text-[11px] tabular-nums select-none ${className ?? ""}`}
    >
      {messageTime(ts)}
    </time>
  )
}

// A line between messages sent on different days.
export const DayDivider: FC<{ label: string }> = ({ label }) => (
  <div data-slot="aui_day-divider" role="separator" aria-label={label} className="text-muted-foreground flex items-center gap-3 text-xs">
    <span className="bg-border h-px flex-1" />
    <span className="first-letter:uppercase">{label}</span>
    <span className="bg-border h-px flex-1" />
  </div>
)
