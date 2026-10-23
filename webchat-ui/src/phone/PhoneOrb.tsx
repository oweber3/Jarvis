import { MessagesSquareIcon } from "lucide-react"
import type { FC } from "react"
import { cn } from "@/lib/utils"
import { Orb } from "@/phone/Orb"
import { orbStateFor } from "@/phone/orbState"
import { usePhone, useVoice } from "@/phone/PhoneProvider"
import { useJarvis } from "@/useJarvis"

// The orb and what goes with it: what the phone is doing in words, a refusal in plain words, and the
// conversation-mode switch. Tapping the orb talks, ends a recording or interrupts Jarvis. While the keyboard is
// up or a confirmation waits it shrinks to one row, so the conversation keeps its room.

const CAPTIONS: Record<string, string> = {
  listening: "Listening. Stop talking, or tap, to send.",
  sending: "Sending...",
  waiting: "Jarvis is working on it. Tap to stop.",
  speaking: "Tap to interrupt.",
}

export const PhoneOrb: FC<{ compact: boolean }> = ({ compact }) => {
  const { reachable, ready } = useJarvis()
  const { pcState } = usePhone()
  const voice = useVoice()
  const { phase, message, conversation } = voice.view
  const { state, label } = orbStateFor({
    reachable,
    ready,
    pcState,
    phoneListening: phase === "listening",
    phoneSpeaking: phase === "speaking",
  })
  const caption = message ?? CAPTIONS[phase] ?? (compact ? null : "Tap the orb to talk")

  const toggle = (
    <button
      type="button"
      role="switch"
      aria-checked={conversation}
      aria-label="Conversation mode"
      title="Conversation mode: listen again after Jarvis speaks"
      onClick={() => voice.setConversation(!conversation)}
      className={cn(
        "flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-full border transition-colors",
        conversation ? "bg-primary text-primary-foreground border-primary" : "text-muted-foreground border-border hover:bg-muted",
      )}
    >
      <MessagesSquareIcon className="size-4" aria-hidden />
    </button>
  )

  return (
    <section aria-label="Jarvis" className="flex flex-col items-center gap-1 px-3">
      <div className={cn("relative w-full", compact ? "" : "h-[26dvh] max-h-72 min-h-40")}>
        <Orb
          state={state}
          label={label}
          level={voice.level}
          compact={compact}
          onTap={voice.tap}
          ariaLabel={phase === "listening" ? "Stop and send" : phase === "speaking" ? "Interrupt Jarvis" : "Talk to Jarvis"}
        />
        {compact && <div className="absolute end-1 top-1/2 -translate-y-1/2">{toggle}</div>}
      </div>
      {(caption || !compact) && (
        <div className="flex min-h-8 w-full items-center justify-center gap-2">
          {caption && (
            <p role="status" className={cn("text-center text-xs", message ? "text-warning" : "text-muted-foreground")}>
              {caption}
            </p>
          )}
          {!compact && toggle}
        </div>
      )}
    </section>
  )
}
