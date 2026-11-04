import { LogOutIcon, SettingsIcon } from "lucide-react"
import { useEffect, useState, type FC, type ReactNode } from "react"
import { ConfirmDialog } from "@/components/ConfirmDialog"
import { ModelPicker } from "@/components/ModelPicker"
import { Button } from "@/components/ui/button"
import { usePhone } from "@/phone/PhoneProvider"
import { Sheet } from "@/phone/Sheet"
import { voiceMessage } from "@/phone/voiceSession"
import { applyTheme, readStoredTheme, storeTheme, type Theme } from "@/theme"

// The settings sheet behind the header's gear: who answers and which model, light or dark, reading replies
// aloud, what voice can do now, and unpairing. The switches are kept on this phone only.

export const PhoneSettings: FC = () => {
  const { phone, prefs, setSpeakReplies, unpair } = usePhone()
  const [open, setOpen] = useState(false)
  const [leaving, setLeaving] = useState(false)
  const [theme, setTheme] = useState<Theme>(() => readStoredTheme())
  const voiceReady = window.isSecureContext ? phone?.voice.available : false
  const voiceReason = window.isSecureContext ? phone?.voice.reason : "no_https"

  useEffect(() => {
    applyTheme(theme)
    storeTheme(theme)
  }, [theme])

  return (
    <>
      <button
        type="button"
        aria-label="Settings"
        aria-haspopup="dialog"
        onClick={() => setOpen(true)}
        className="text-muted-foreground hover:bg-muted hover:text-foreground flex size-11 shrink-0 items-center justify-center rounded-full"
      >
        <SettingsIcon className="size-5" aria-hidden />
      </button>
      <Sheet open={open} onOpenChange={setOpen} title="Settings" description="Switches here are kept on this phone only.">
        <div className="flex flex-col gap-5 pb-2 text-sm">
          <Section title="Who answers">
            <div className="flex flex-wrap items-center gap-2">
              <ModelPicker />
            </div>
          </Section>
          <Section title="Appearance">
            <Row label="Light mode" help="Dark is the Jarvis look." checked={theme === "light"} onChange={(on) => setTheme(on ? "light" : "dark")} />
          </Section>
          <Section title="Voice">
            <Row
              label="Read replies aloud"
              help="Speak the answer to messages you type, in Jarvis's voice, on this phone."
              checked={prefs.speakReplies}
              onChange={setSpeakReplies}
            />
            <p className="text-muted-foreground text-xs" role="status">
              {voiceReady ? "Voice is ready." : voiceMessage(voiceReason ?? "")}
            </p>
          </Section>
          <Button type="button" variant="outline" className="h-11 justify-start gap-2 rounded-xl" onClick={() => setLeaving(true)}>
            <LogOutIcon className="size-4" aria-hidden />
            Unpair this phone
          </Button>
        </div>
      </Sheet>
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

const Section: FC<{ title: string; children: ReactNode }> = ({ title, children }) => (
  <section className="flex flex-col gap-3">
    <h3 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">{title}</h3>
    {children}
  </section>
)

const Row: FC<{ label: string; help: ReactNode; checked: boolean; onChange: (on: boolean) => void }> = ({
  label,
  help,
  checked,
  onChange,
}) => (
  <div className="flex items-center justify-between gap-4">
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
      className="flex min-h-11 min-w-14 shrink-0 items-center justify-center"
    >
      <span
        aria-hidden
        className={`relative block h-7 w-12 rounded-full border transition-colors ${checked ? "bg-primary border-primary" : "bg-muted border-input"}`}
      >
        <span className={`absolute top-0.5 size-5 rounded-full shadow transition-all ${checked ? "bg-background start-[1.625rem]" : "bg-muted-foreground start-0.5"}`} />
      </span>
    </button>
  </div>
)
