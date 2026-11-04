import { XIcon } from "lucide-react"
import { useEffect, useRef, useState, type FC } from "react"
import { cn } from "@/lib/utils"
import { ConfirmCard } from "@/phone/ConfirmCard"
import { pendingConfirmation } from "@/phone/confirmation"
import { usePhone, useVoice } from "@/phone/PhoneProvider"
import { PhoneToast } from "@/phone/PhoneToast"
import { useKeyboardIsOpen } from "@/phone/useKeyboardOpen"
import { inlineVoiceStatus } from "@/phone/voiceMode"
import type { VoiceView } from "@/phone/voiceSession"
import { useJarvis } from "@/useJarvis"

// The stack just above the message box, inside the thread's sticky footer, so it never covers the message box
// or the keyboard and the conversation scrolls behind it: the indicator of a request spoken with the microphone
// button, a pending desktop confirmation, and the toast. While voice mode is open, voice mode shows them.

export const PhoneDock: FC = () => {
  const { toast, actions } = useJarvis()
  const { phone, answer } = usePhone()
  const voice = useVoice()
  const keyboardOpen = useKeyboardIsOpen()
  const confirmation = pendingConfirmation(phone)
  if (voice.voiceMode) return null
  return (
    <div className="phone-dock flex flex-col gap-2 empty:hidden">
      <InlineVoiceStatus view={voice.view} level={voice.level} />
      {confirmation && (
        <ConfirmCard confirmation={confirmation} answer={answer} onProblem={actions.showToast} compact={keyboardOpen} />
      )}
      {toast && <PhoneToast message={toast} onDismiss={actions.dismissToast} />}
    </div>
  )
}

const DOTS = { live: "bg-destructive", working: "bg-primary", warn: "bg-warning" } as const

const InlineVoiceStatus: FC<{ view: VoiceView; level: () => number | null }> = ({ view, level }) => {
  // A refusal stays until the next request or until it is dismissed.
  const [dismissed, setDismissed] = useState<VoiceView | null>(null)
  const status = view === dismissed ? null : inlineVoiceStatus(view)
  const meter = useRef<HTMLSpanElement>(null)
  const live = status?.tone === "live"

  // While recording, a small bar follows the microphone's level (no orb on this screen).
  useEffect(() => {
    if (!live) return
    let frame = 0
    const draw = () => {
      const value = Math.max(0.08, Math.min(1, level() ?? 0))
      if (meter.current) meter.current.style.transform = `scaleX(${value})`
      frame = requestAnimationFrame(draw)
    }
    frame = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(frame)
  }, [live, level])

  if (!status) return null
  return (
    <div
      role="status"
      className={cn(
        "bg-popover border-border flex min-h-11 items-center gap-3 self-center rounded-full border py-1 ps-4 text-sm shadow-md",
        status.tone === "warn" ? "pe-1" : "pe-4",
      )}
    >
      <span aria-hidden className="relative flex size-2.5 shrink-0">
        {status.tone !== "warn" && <span className={cn("absolute inset-0 animate-ping rounded-full opacity-70", DOTS[status.tone])} />}
        <span className={cn("relative size-2.5 rounded-full", DOTS[status.tone])} />
      </span>
      <span className={cn("min-w-0", status.tone === "warn" && "text-warning")}>{status.text}</span>
      {live && (
        <span aria-hidden className="bg-muted h-1.5 w-12 shrink-0 overflow-hidden rounded-full">
          <span ref={meter} className="bg-destructive block h-full w-full origin-left rounded-full" style={{ transform: "scaleX(0.08)" }} />
        </span>
      )}
      {status.tone === "warn" && (
        <button
          type="button"
          aria-label="Dismiss"
          onClick={() => setDismissed(view)}
          className="text-muted-foreground hover:text-foreground flex size-9 shrink-0 items-center justify-center rounded-full"
        >
          <XIcon className="size-4" aria-hidden />
        </button>
      )}
    </div>
  )
}
