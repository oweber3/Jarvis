// The header's presence line: whether Jarvis can be reached and what it is doing, as the desktop chat's contact
// header says "Online", "Typing…" or "Offline". Pure, covered by tests.

export type PresenceInput = {
  reachable: boolean
  ready: boolean
  /** The assistant's state as the poll reports it (``thinking`` while a typed request runs). */
  state: string
  /** A request holds the reply engine (voice or typed). */
  busy: boolean
  /** A typed request from this page or a phone is being answered. */
  working: boolean
}

export type Presence = { tone: "online" | "working" | "waiting" | "offline"; label: string }

const DOING: Record<string, string> = {
  thinking: "Thinking",
  speaking: "Speaking",
  listening: "Listening",
  dictating: "Taking dictation",
  dictation_processing: "Taking dictation",
}

export function presence(input: PresenceInput): Presence {
  if (!input.reachable) return { tone: "offline", label: "Offline" }
  if (!input.ready) return { tone: "waiting", label: "Starting" }
  if (input.working) return { tone: "working", label: "Thinking" }
  const doing = DOING[input.state]
  if (doing) return { tone: "working", label: doing }
  if (input.busy) return { tone: "working", label: "Busy" }
  return { tone: "online", label: "Online" }
}
