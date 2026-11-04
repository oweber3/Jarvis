import { ShieldQuestionIcon } from "lucide-react"
import { useEffect, useRef, useState, type FC } from "react"
import type { Confirmation } from "@/api"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { deadlineFor, secondsLeft } from "@/phone/countdown"

// A desktop confirmation waiting for a yes or no: what Jarvis wants to do, with Approve and Deny and the time
// left. Answering here or on the desktop resolves the same request; whichever comes first wins. It sits just
// above the message box, and over voice mode while that is open. `compact` drops the consequence line while
// the keyboard is up, so the conversation keeps its room.

export type ConfirmCardProps = {
  confirmation: Confirmation
  /** Answers it; a plain sentence when it could not be answered, else null. */
  answer: (id: string, approve: boolean) => Promise<string | null>
  onProblem: (message: string) => void
  compact?: boolean
  className?: string
}

export const ConfirmCard: FC<ConfirmCardProps> = ({ confirmation, answer, onProblem, compact = false, className }) => {
  const deadline = useRef({ id: "", at: 0 })
  const [now, setNow] = useState(() => Date.now())
  const [busy, setBusy] = useState(false)

  if (deadline.current.id !== confirmation.id) {
    deadline.current = { id: confirmation.id, at: deadlineFor(confirmation.expires_in, Date.now()) }
  }

  useEffect(() => {
    setBusy(false)
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [confirmation.id])

  const left = secondsLeft(deadline.current.at, now)

  const respond = async (approve: boolean) => {
    setBusy(true)
    const problem = await answer(confirmation.id, approve)
    if (problem) onProblem(problem)
    setBusy(false)
  }

  return (
    <section
      role="alertdialog"
      aria-label="Jarvis is asking for confirmation"
      className={cn("border-warning/50 bg-popover flex flex-col gap-2 rounded-2xl border p-3 shadow-lg", className)}
    >
      <div className="flex items-start gap-2">
        <ShieldQuestionIcon aria-hidden className="text-warning mt-0.5 size-5 shrink-0" />
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <p className={cn("text-sm font-medium break-words", compact && "line-clamp-2")}>
            {confirmation.action}
            {confirmation.target ? `: ${confirmation.target}` : ""}
          </p>
          {confirmation.consequence && !compact && (
            <p className="text-muted-foreground text-xs break-words">{confirmation.consequence}</p>
          )}
        </div>
        <span aria-live="off" className="text-muted-foreground shrink-0 text-sm tabular-nums" title="Time left to answer">
          {left}s
        </span>
      </div>
      <div className="flex items-center gap-2">
        <Button type="button" disabled={busy || left === 0} onClick={() => void respond(true)} className="h-11 flex-1 rounded-xl">
          Approve
        </Button>
        <Button
          type="button"
          variant="outline"
          disabled={busy || left === 0}
          onClick={() => void respond(false)}
          className="h-11 flex-1 rounded-xl"
        >
          Deny
        </Button>
      </div>
    </section>
  )
}
