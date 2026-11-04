import { MessagesSquareIcon, XIcon } from "lucide-react"
import type { ComponentType, FC, ReactNode } from "react"
import type { Confirmation } from "@/api"
import { cn } from "@/lib/utils"
import { ConfirmCard } from "@/phone/ConfirmCard"
import { Orb } from "@/phone/Orb"
import type { OrbView } from "@/phone/orbState"
import { PhoneToast } from "@/phone/PhoneToast"
import { voiceModeCaption, type Exchange } from "@/phone/voiceMode"
import type { VoiceView } from "@/phone/voiceSession"

// What voice mode shows, free of the page's providers: the orb large in the middle with its caption, the last
// exchange as short text under it, a pending confirmation and any toast above the two big buttons
// (conversation mode, and close). VoiceModeDialog wraps it in the full-screen dialog.

export type VoiceModeScreenProps = {
  view: VoiceView
  orb: OrbView
  level: () => number | null
  exchange: Exchange
  confirmation: Confirmation | null
  answer: (id: string, approve: boolean) => Promise<string | null>
  onProblem: (message: string) => void
  toast: string | null
  onDismissToast: () => void
  onTap: () => void
  onConversation: (on: boolean) => void
  onClose: () => void
  /** The dialog's title element (the dialog passes its own, so it is announced). */
  Title?: ComponentType<{ className?: string; children: ReactNode }>
}

const DefaultTitle: FC<{ className?: string; children: ReactNode }> = (props) => <h2 {...props} />

const ORB_LABELS: Partial<Record<VoiceView["phase"], string>> = {
  listening: "Stop and send",
  sending: "Stop",
  waiting: "Stop",
  speaking: "Interrupt Jarvis",
}

export const VoiceModeScreen: FC<VoiceModeScreenProps> = ({
  view,
  orb,
  level,
  exchange,
  confirmation,
  answer,
  onProblem,
  toast,
  onDismissToast,
  onTap,
  onConversation,
  onClose,
  Title = DefaultTitle,
}) => {
  const caption = voiceModeCaption(view)
  return (
    <div className="voice-mode flex h-full w-full flex-col items-center">
      <Title className="text-muted-foreground pt-2 text-xs font-medium tracking-[0.2em] uppercase">Voice mode</Title>

      <div className="flex min-h-0 w-full max-w-md flex-1 flex-col items-center justify-center gap-3 px-4 py-3">
        <div className="relative min-h-44 w-full flex-1" style={{ maxHeight: "min(56vh, 26rem)" }}>
          <Orb
            state={orb.state}
            label={orb.label}
            level={level}
            compact={false}
            onTap={onTap}
            ariaLabel={ORB_LABELS[view.phase] ?? "Talk to Jarvis"}
          />
        </div>
        <p role="status" className={cn("min-h-5 text-center text-sm", view.message ? "text-warning" : "text-muted-foreground")}>
          {caption}
        </p>
        {(exchange.said || exchange.reply) && (
          <div aria-label="Latest exchange" className="flex w-full flex-col items-center gap-1 text-center">
            {exchange.said && (
              <p className="text-muted-foreground line-clamp-2 text-sm break-words">
                <span className="sr-only">You said: </span>
                {exchange.said}
              </p>
            )}
            {exchange.reply && (
              <p className="text-foreground line-clamp-3 text-base leading-snug break-words">
                <span className="sr-only">Jarvis said: </span>
                {exchange.reply}
              </p>
            )}
          </div>
        )}
      </div>

      {(confirmation || toast) && (
        <div className="flex w-full max-w-md flex-col gap-2 px-4 pb-3">
          {confirmation && <ConfirmCard confirmation={confirmation} answer={answer} onProblem={onProblem} />}
          {toast && <PhoneToast message={toast} onDismiss={onDismissToast} />}
        </div>
      )}

      <div className="flex w-full max-w-xs items-start justify-between px-2 pb-2">
        <div className="flex w-24 flex-col items-center gap-1.5">
          <button
            type="button"
            role="switch"
            aria-checked={view.conversation}
            aria-label="Conversation mode"
            onClick={() => onConversation(!view.conversation)}
            className={cn(
              "flex size-16 items-center justify-center rounded-full border transition-colors",
              view.conversation
                ? "bg-primary text-primary-foreground border-primary"
                : "bg-muted text-muted-foreground border-border hover:text-foreground",
            )}
          >
            <MessagesSquareIcon className="size-6" aria-hidden />
          </button>
          <span aria-hidden className="text-muted-foreground text-center text-xs whitespace-nowrap">
            {view.conversation ? "Conversation on" : "Conversation off"}
          </span>
        </div>
        <div className="flex w-24 flex-col items-center gap-1.5">
          <button
            type="button"
            aria-label="End voice mode"
            onClick={onClose}
            className="bg-muted text-foreground border-border hover:bg-destructive hover:text-white flex size-16 items-center justify-center rounded-full border transition-colors"
          >
            <XIcon className="size-7" aria-hidden />
          </button>
          <span aria-hidden className="text-muted-foreground text-xs">
            End
          </span>
        </div>
      </div>
    </div>
  )
}
