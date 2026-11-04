import type { VoiceView } from "@/phone/voiceSession"

// Voice mode: the full-screen view for talking to Jarvis hands-free, with no React in it so it can be tested.
// Opening it is the owner's tap, so listening starts in that same gesture (iOS unlocks audio only inside one)
// and conversation mode is on: after Jarvis speaks the page listens again. Closing it, leaving the page or
// backgrounding it stops the recording and the playback and lets go of the microphone. Outside voice mode
// conversation mode is off, so the message box's microphone button makes one request.
// See remote/remote.spec.md, The phone app.

/** What voice mode needs of the voice session (`VoiceSession`). */
export type VoiceModeSession = {
  readonly view: VoiceView
  tap(): void
  setConversation(on: boolean): void
  stop(): void
}

export class VoiceMode {
  private shown = false

  constructor(
    private readonly session: VoiceModeSession,
    private readonly onChange: (open: boolean) => void = () => undefined,
  ) {}

  get open(): boolean {
    return this.shown
  }

  /** Open voice mode inside the owner's tap: conversation mode on, and listening at once unless Jarvis is busy talking. */
  enter(): void {
    if (this.shown) return
    this.shown = true
    this.session.setConversation(true)
    // A reply already being read aloud plays on; conversation mode then listens after it.
    if (this.session.view.phase === "idle") this.session.tap()
    this.onChange(true)
  }

  /** Close voice mode, or let go of the voice when the page is hidden: stop everything and release the microphone. */
  leave(): void {
    const was = this.shown
    this.shown = false
    this.session.setConversation(false)
    this.session.stop()
    if (was) this.onChange(false)
  }
}

type PageEvents = Pick<EventTarget, "addEventListener" | "removeEventListener"> & { readonly visibilityState: string }

/** Calls `leave` when the page is backgrounded or left. Returns the function that stops watching. */
export function leaveWhenHidden(page: PageEvents, win: Pick<EventTarget, "addEventListener" | "removeEventListener">, leave: () => void): () => void {
  const onVisibility = () => {
    if (page.visibilityState === "hidden") leave()
  }
  page.addEventListener("visibilitychange", onVisibility)
  win.addEventListener("pagehide", leave)
  return () => {
    page.removeEventListener("visibilitychange", onVisibility)
    win.removeEventListener("pagehide", leave)
  }
}

const CAPTIONS: Record<VoiceView["phase"], string> = {
  idle: "Tap the orb to talk",
  listening: "Listening. Stop talking, or tap, to send.",
  sending: "Sending...",
  waiting: "Jarvis is working on it. Tap to stop.",
  speaking: "Tap to interrupt.",
}

/** The line under the orb in voice mode: a refusal in plain words, or what the owner can do now. */
export function voiceModeCaption(view: VoiceView): string {
  return view.message ?? CAPTIONS[view.phase]
}

export type InlineStatus = { text: string; tone: "live" | "working" | "warn" }

const INLINE: Partial<Record<VoiceView["phase"], InlineStatus>> = {
  listening: { text: "Listening. Tap the square to send.", tone: "live" },
  sending: { text: "Sending...", tone: "working" },
  waiting: { text: "Jarvis is working on it.", tone: "working" },
  speaking: { text: "Jarvis is speaking.", tone: "working" },
}

/** The small indicator above the message box for a request spoken with its microphone button, or null. */
export function inlineVoiceStatus(view: VoiceView): InlineStatus | null {
  if (view.phase === "idle") return view.message ? { text: view.message, tone: "warn" } : null
  return INLINE[view.phase] ?? null
}

export type ExchangeMessage = { role: "user" | "assistant" | "system"; text: string; notice: boolean }

export type Exchange = { said: string | null; reply: string | null }

/** The owner's last message and Jarvis's answer to it (none yet while it works), for voice mode to show. */
export function latestExchange(messages: readonly ExchangeMessage[]): Exchange {
  const real = messages.filter((m) => !m.notice && m.text.trim() !== "" && m.role !== "system")
  let lastUser = -1
  for (let i = real.length - 1; i >= 0; i--) {
    if (real[i]!.role === "user") {
      lastUser = i
      break
    }
  }
  if (lastUser < 0) return { said: null, reply: null }
  const replies = real.slice(lastUser + 1).filter((m) => m.role === "assistant")
  const reply = replies.length > 0 ? replies[replies.length - 1]!.text.trim() : null
  return { said: real[lastUser]!.text.trim(), reply }
}
