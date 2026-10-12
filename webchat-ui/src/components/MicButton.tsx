import { useAui } from "@assistant-ui/react"
import { LoaderCircleIcon, MicIcon, SquareIcon } from "lucide-react"
import { useCallback, useEffect, useState, type FC } from "react"
import { TooltipIconButton } from "@/components/tooltip-icon-button"
import { ApiError } from "@/api"
import { cn } from "@/lib/utils"
import { MicRecorder, describeMicError, formatElapsed, isLastingRefusal, micApi, type MicView } from "@/mic"

// The microphone button in the message box. Click to start, click again (or Esc) to stop; the words are
// added to the message for the owner to read and send. Jarvis does the recording and the speech
// recognition on this PC (jarvis/webchat/mic.py), so the page never asks the browser for the microphone.

const IDLE: MicView = { phase: "idle", elapsed: 0, maxSeconds: 0, message: null }
const NOTICE_MS = 7000

export const MicButton: FC = () => {
  const aui = useAui()
  const [recorder, setRecorder] = useState<MicRecorder | null>(null)
  const [view, setView] = useState<MicView>(IDLE)
  const [lasting, setLasting] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  // The words go after what is already typed.
  const addToMessage = useCallback(
    (text: string) => {
      const composer = aui.composer()
      const typed = composer.getState().text
      composer.setText(typed && !/\s$/.test(typed) ? `${typed} ${text}` : `${typed}${text}`)
      requestAnimationFrame(() => {
        const input = document.querySelector<HTMLTextAreaElement>('[aria-label="Message input"]')
        input?.focus()
        input?.setSelectionRange(input.value.length, input.value.length)
      })
    },
    [aui],
  )

  useEffect(() => {
    const created = new MicRecorder(micApi, addToMessage, setView)
    setRecorder(created)
    // Leaving the page releases the microphone at once; the daemon would also do so when the beats stop.
    const leave = () => created.cancel()
    window.addEventListener("pagehide", leave)
    return () => {
      window.removeEventListener("pagehide", leave)
      created.dispose()
      setRecorder(null)
    }
  }, [addToMessage])

  // Dictation turned off or not set up is said before the first click, not after it.
  useEffect(() => {
    let current = true
    const check = () =>
      micApi
        .status()
        .then((status) => current && setLasting(isLastingRefusal(status.reason) ? status.reason : null))
        .catch(() => undefined)
    void check()
    window.addEventListener("focus", check)
    return () => {
      current = false
      window.removeEventListener("focus", check)
    }
  }, [])

  // Esc stops the recording, ahead of whatever else would use the key.
  const recording = view.phase === "recording"
  useEffect(() => {
    if (!recording || !recorder) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return
      event.preventDefault()
      event.stopPropagation()
      void recorder.stop()
    }
    window.addEventListener("keydown", onKey, true)
    return () => window.removeEventListener("keydown", onKey, true)
  }, [recording, recorder])

  useEffect(() => {
    setNotice(view.message)
    if (!view.message) return
    const timer = setTimeout(() => setNotice(null), NOTICE_MS)
    return () => clearTimeout(timer)
  }, [view.message])

  const busy = view.phase === "starting" || view.phase === "transcribing"
  const unavailable = lasting !== null
  const label = recording
    ? "Stop recording (Esc)"
    : view.phase === "transcribing"
      ? "Writing out what you said"
      : unavailable
        ? describeMicError(new ApiError(503, lasting ?? ""))
        : "Speak instead of typing"

  return (
    <div className="flex items-center gap-1.5">
      {notice && (
        <p role="status" className="bg-popover text-popover-foreground border-border absolute right-0 bottom-full z-10 mb-2 max-w-xs rounded-lg border px-3 py-1.5 text-xs shadow-md">
          {notice}
        </p>
      )}
      {recording && (
        <span className="text-destructive flex shrink-0 items-center gap-1.5 text-xs font-medium whitespace-nowrap tabular-nums" data-testid="mic-timer">
          <span aria-hidden className="bg-destructive size-2 shrink-0 rounded-full motion-safe:animate-pulse" />
          <span>
            {formatElapsed(view.elapsed)}
            <span className="text-muted-foreground max-sm:hidden"> / {formatElapsed(view.maxSeconds)}</span>
          </span>
        </span>
      )}
      {view.phase === "transcribing" && <span className="text-muted-foreground text-xs max-sm:hidden">Transcribing</span>}
      <TooltipIconButton
        tooltip={label}
        side="bottom"
        type="button"
        variant={recording ? "default" : "ghost"}
        size="icon"
        aria-pressed={recording}
        aria-label={label}
        disabled={!recorder || busy || (unavailable && !recording)}
        onClick={() => void recorder?.toggle()}
        className={cn("aui-composer-mic size-7 rounded-full", recording && "bg-destructive text-background hover:bg-destructive/90")}
      >
        {recording ? (
          <SquareIcon className="size-3 fill-current" />
        ) : busy ? (
          <LoaderCircleIcon className="size-4 motion-safe:animate-spin" />
        ) : (
          <MicIcon className="size-4" />
        )}
      </TooltipIconButton>
    </div>
  )
}
