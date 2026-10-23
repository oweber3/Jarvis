import { ShieldQuestionIcon } from "lucide-react"
import { useEffect, useRef, useState, type FC } from "react"
import { Button } from "@/components/ui/button"
import { deadlineFor, secondsLeft } from "@/phone/countdown"
import { usePhone } from "@/phone/PhoneProvider"
import { useJarvis } from "@/useJarvis"

// A desktop confirmation waiting for a yes or no: what Jarvis wants to do, with Approve and Deny and the time
// left. Answering here or on the desktop resolves the same request; whichever comes first wins.

export const ConfirmCard: FC = () => {
  const { phone, answer } = usePhone()
  const { actions } = useJarvis()
  const confirmation = phone?.allow_confirm ? phone.confirmation : null
  const deadline = useRef({ id: "", at: 0 })
  const [now, setNow] = useState(() => Date.now())
  const [busy, setBusy] = useState(false)

  if (confirmation && deadline.current.id !== confirmation.id) {
    deadline.current = { id: confirmation.id, at: deadlineFor(confirmation.expires_in, Date.now()) }
  }

  useEffect(() => {
    if (!confirmation) return
    setBusy(false)
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [confirmation?.id])

  if (!confirmation) return null
  const left = secondsLeft(deadline.current.at, now)

  const respond = async (approve: boolean) => {
    setBusy(true)
    const problem = await answer(confirmation.id, approve)
    if (problem) actions.showToast(problem)
    setBusy(false)
  }

  return (
    <section
      role="alertdialog"
      aria-label="Jarvis is asking for confirmation"
      className="border-warning/40 bg-warning/10 mx-3 mb-2 flex flex-col gap-2 rounded-xl border p-3"
    >
      <div className="flex items-start gap-2">
        <ShieldQuestionIcon aria-hidden className="text-warning mt-0.5 size-5 shrink-0" />
        <div className="flex min-w-0 flex-col gap-0.5">
          <p className="text-sm font-medium break-words">
            {confirmation.action}
            {confirmation.target ? `: ${confirmation.target}` : ""}
          </p>
          {confirmation.consequence && <p className="text-muted-foreground text-xs break-words">{confirmation.consequence}</p>}
        </div>
      </div>
      <div className="flex items-center gap-2">
        <Button type="button" disabled={busy || left === 0} onClick={() => void respond(true)} className="h-11 flex-1">
          Approve
        </Button>
        <Button type="button" variant="outline" disabled={busy || left === 0} onClick={() => void respond(false)} className="h-11 flex-1">
          Deny
        </Button>
        <span aria-live="off" className="text-muted-foreground w-12 text-end text-sm tabular-nums" title="Time left to answer">
          {left}s
        </span>
      </div>
    </section>
  )
}
