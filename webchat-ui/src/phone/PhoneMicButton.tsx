import { MicIcon, SquareIcon } from "lucide-react"
import type { FC } from "react"
import { cn } from "@/lib/utils"
import { useVoice } from "@/phone/PhoneProvider"

// The microphone button in the message box: the same as tapping the orb. It records with this phone's own
// microphone and sends the request when the owner stops talking (the PC's microphone is not used).

export const PhoneMicButton: FC = () => {
  const { view, tap } = useVoice()
  const active = view.phase !== "idle"
  const label =
    view.phase === "listening" ? "Stop and send" : view.phase === "speaking" ? "Interrupt Jarvis" : active ? "Stop" : "Talk to Jarvis"
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      aria-pressed={active}
      onClick={tap}
      className={cn(
        "flex size-11 items-center justify-center rounded-full transition-colors",
        view.phase === "listening" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground",
      )}
    >
      {active ? <SquareIcon className="size-4 fill-current" aria-hidden /> : <MicIcon className="size-5" aria-hidden />}
    </button>
  )
}
