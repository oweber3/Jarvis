import type { OrbState } from "@/phone/orbModel"

// Which state the phone's orb shows. The PC's assistant state maps as in desktop_app/orb_widget.py
// (_JARVIS_TO_ORB); what the phone itself is doing, and whether the PC can be reached, come first.

export type OrbInput = {
  /** The PC answered the last request. */
  reachable: boolean
  /** The PC's daemon is ready for requests. */
  ready: boolean
  /** The PC's assistant state value (`asleep`, `idle`, `listening`, ...). */
  pcState: string
  /** The phone's own microphone is recording a request. */
  phoneListening: boolean
  /** The phone is playing a reply. */
  phoneSpeaking: boolean
}

export type OrbView = {
  state: OrbState
  /** Text to show in place of the state's own label, or null to use the look's label. */
  label: string | null
}

const PC_STATES: Record<string, OrbState> = {
  asleep: "offline",
  idle: "idle",
  listening: "listening",
  thinking: "thinking",
  speaking: "speaking",
  dictating: "dictating",
  dictation_processing: "thinking",
}

export function orbStateFor(input: OrbInput): OrbView {
  if (!input.reachable) return { state: "offline", label: "can't reach the PC" }
  if (input.phoneListening) return { state: "listening", label: null }
  if (input.phoneSpeaking) return { state: "speaking", label: null }
  if (!input.ready) return { state: "offline", label: "starting up" }
  return { state: PC_STATES[input.pcState] ?? "offline", label: null }
}

/** The on-screen keyboard shrinks the visual viewport; this much shorter than the layout counts as open. */
export const KEYBOARD_MIN_HEIGHT = 150

export function keyboardOpen(layoutHeight: number, visualHeight: number): boolean {
  return layoutHeight - visualHeight >= KEYBOARD_MIN_HEIGHT
}
