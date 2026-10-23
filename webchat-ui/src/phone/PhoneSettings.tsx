import { LogOutIcon, SettingsIcon } from "lucide-react"
import { useState, type FC, type ReactNode } from "react"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { ConfirmDialog } from "@/components/ConfirmDialog"
import { voiceMessage } from "@/phone/voiceSession"
import { usePhone, useVoice } from "@/phone/PhoneProvider"

// The phone's own settings: reading replies aloud, conversation mode, what voice can do now, and unpairing.

export const PhoneSettings: FC = () => {
  const { phone, prefs, setSpeakReplies, unpair } = usePhone()
  const voice = useVoice()
  const [open, setOpen] = useState(false)
  const [leaving, setLeaving] = useState(false)
  const voiceReady = window.isSecureContext ? phone?.voice.available : false
  const voiceReason = window.isSecureContext ? phone?.voice.reason : "no_https"

  return (
    <>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogTrigger asChild>
          <button
            type="button"
            aria-label="Phone settings"
            className="text-muted-foreground hover:bg-muted hover:text-foreground flex size-8 shrink-0 items-center justify-center rounded-md"
          >
            <SettingsIcon className="size-4" />
          </button>
        </DialogTrigger>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>This phone</DialogTitle>
            <DialogDescription>Settings kept on this phone only.</DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-4 text-sm">
            <Row
              label="Read replies aloud"
              help="Speak the answer to messages you type, in Jarvis's voice, on this phone."
              checked={prefs.speakReplies}
              onChange={setSpeakReplies}
            />
            <Row
              label="Conversation mode"
              help="After Jarvis speaks, listen again by itself. A tap on the orb ends it."
              checked={voice.view.conversation}
              onChange={voice.setConversation}
            />
            <p className="text-muted-foreground" role="status">
              {voiceReady ? "Voice is ready." : voiceMessage(voiceReason ?? "")}
            </p>
            <Button type="button" variant="outline" className="h-11 justify-start gap-2" onClick={() => setLeaving(true)}>
              <LogOutIcon className="size-4" aria-hidden />
              Unpair this phone
            </Button>
          </div>
        </DialogContent>
      </Dialog>
      <ConfirmDialog
        open={leaving}
        onOpenChange={setLeaving}
        title="Unpair this phone?"
        description="This phone is removed from Jarvis on your PC. To use it again, pair it with a new code."
        confirmLabel="Unpair"
        onConfirm={unpair}
      />
    </>
  )
}

const Row: FC<{ label: string; help: ReactNode; checked: boolean; onChange: (on: boolean) => void }> = ({
  label,
  help,
  checked,
  onChange,
}) => (
  <div className="flex items-start justify-between gap-4">
    <div className="flex flex-col gap-0.5">
      <span className="font-medium">{label}</span>
      <span className="text-muted-foreground text-xs">{help}</span>
    </div>
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={`relative mt-0.5 h-7 w-12 shrink-0 rounded-full border transition-colors ${checked ? "bg-primary border-primary" : "bg-muted border-input"}`}
    >
      <span
        aria-hidden
        className={`bg-background absolute top-0.5 size-5 rounded-full shadow transition-all ${checked ? "start-[1.625rem]" : "start-0.5"}`}
      />
    </button>
  </div>
)
