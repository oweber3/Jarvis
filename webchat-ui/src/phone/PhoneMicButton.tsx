import { AudioLinesIcon, MicIcon, SquareIcon } from "lucide-react"
import type { FC } from "react"
import { cn } from "@/lib/utils"
import { useVoice } from "@/phone/PhoneProvider"

// The buttons by the message box that talk to Jarvis with this phone's own microphone (the PC's microphone is
// not used). The microphone button makes one spoken request, sent when the owner stops talking, with a small
// indicator above the message box instead of the orb. The voice mode button opens the full-screen voice mode.

export const PhoneMicButton: FC = () => {
  const { view, tap } = useVoice()
  const active = view.phase !== "idle"
  const label =
    view.phase === "listening" ? "Stop and send" : view.phase === "speaking" ? "Interrupt Jarvis" : active ? "Stop" : "Speak one request"
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      aria-pressed={active}
      onClick={tap}
      className={cn(
        "flex size-11 items-center justify-center rounded-full transition-colors",
        view.phase === "listening" ? "bg-destructive text-white" : active ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground",
      )}
    >
      {active ? <SquareIcon className="size-4 fill-current" aria-hidden /> : <MicIcon className="size-5" aria-hidden />}
    </button>
  )
}

export const VoiceModeButton: FC = () => {
  const { enterVoiceMode } = useVoice()
  return (
    <button
      type="button"
      data-voice-mode-button
      aria-label="Open voice mode"
      title="Voice mode"
      aria-haspopup="dialog"
      onClick={enterVoiceMode}
      className="bg-foreground/10 text-foreground hover:bg-foreground/15 flex size-11 items-center justify-center rounded-full transition-colors"
    >
      <AudioLinesIcon className="size-5" aria-hidden />
    </button>
  )
}

export const PhoneComposerVoice: FC = () => (
  <>
    <PhoneMicButton />
    <VoiceModeButton />
  </>
)
