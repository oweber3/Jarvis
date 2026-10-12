import { useSyncExternalStore } from "react"

// "Show me this message": set when a search result is opened, taken by the thread once the message is on
// screen. A tiny external store so the sidebar and the thread need not know about each other.

let target: number | null = null
const listeners = new Set<() => void>()

export const scrollTarget = {
  set(messageId: number | null) {
    target = messageId
    listeners.forEach((listener) => listener())
  },
  get: () => target,
  subscribe(listener: () => void) {
    listeners.add(listener)
    return () => listeners.delete(listener)
  },
}

export const useScrollTarget = () => useSyncExternalStore(scrollTarget.subscribe, scrollTarget.get)

// assistant-ui marks each message element with ``data-message-id``; the thread's ids are "m<stored id>".
export const messageSelector = (storedId: number): string => `[data-message-id="m${storedId}"]`
