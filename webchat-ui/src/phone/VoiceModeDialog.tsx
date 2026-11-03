import { useAuiState } from "@assistant-ui/react"
import { Dialog as DialogPrimitive } from "radix-ui"
import { useMemo, type FC } from "react"
import { pendingConfirmation } from "@/phone/confirmation"
import { orbStateFor } from "@/phone/orbState"
import { usePhone, useVoice } from "@/phone/PhoneProvider"
import { latestExchange, type ExchangeMessage } from "@/phone/voiceMode"
import { VoiceModeScreen } from "@/phone/VoiceModeScreen"
import { useJarvis } from "@/useJarvis"

// Voice mode as a full-screen modal dialog over the chat. Focus moves into it when it opens and back to the
// voice mode button when it closes; Esc and the close button end it, which stops recording and playback and
// releases the microphone (VoiceMode.leave).

type PartLike = { type: string; text?: string }

export const VoiceModeDialog: FC = () => {
  const voice = useVoice()
  const { reachable, ready, toast, actions } = useJarvis()
  const { phone, pcState, answer } = usePhone()
  const { phase } = voice.view
  const orb = orbStateFor({ reachable, ready, pcState, phoneListening: phase === "listening", phoneSpeaking: phase === "speaking" })
  const messages = useAuiState((s) => s.thread.messages)
  const exchange = useMemo(
    () =>
      latestExchange(
        messages.map(
          (m): ExchangeMessage => ({
            role: m.role,
            text: (m.content as readonly PartLike[])
              .map((part) => (part.type === "text" ? (part.text ?? "") : ""))
              .join(""),
            notice: m.metadata.custom?.["notice"] === true,
          }),
        ),
      ),
    [messages],
  )

  return (
    <DialogPrimitive.Root
      open={voice.voiceMode}
      onOpenChange={(open) => {
        if (!open) voice.leaveVoiceMode()
      }}
    >
      <DialogPrimitive.Portal>
        <DialogPrimitive.Content
          aria-describedby={undefined}
          className="phone-voice-mode bg-background text-foreground data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95 fixed inset-0 z-50 flex outline-none duration-200"
          onOpenAutoFocus={(event) => {
            // The end button, not the orb, so a stray keyboard Enter cannot start or stop a recording.
            event.preventDefault()
            ;(event.currentTarget as HTMLElement | null)?.querySelector<HTMLElement>('[aria-label="End voice mode"]')?.focus()
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault()
            document.querySelector<HTMLElement>("[data-voice-mode-button]")?.focus()
          }}
        >
          <VoiceModeScreen
            view={voice.view}
            orb={orb}
            level={voice.level}
            exchange={exchange}
            confirmation={pendingConfirmation(phone)}
            answer={answer}
            onProblem={actions.showToast}
            toast={toast}
            onDismissToast={actions.dismissToast}
            onTap={voice.tap}
            onConversation={voice.setConversation}
            onClose={voice.leaveVoiceMode}
            Title={DialogPrimitive.Title}
          />
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}
