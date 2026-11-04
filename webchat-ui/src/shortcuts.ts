// Keyboard shortcuts of the page. Pure: the decision is made here, the page performs it.
//
// Nothing here uses a plain letter, so typing is never captured: "?" and "/" are taken only outside text
// fields, and the up arrow only in an empty message box. Ctrl/Cmd combinations work in the message box too,
// because that is where the owner usually is. Esc is only claimed when it has no other job at that moment
// (closing a dialog or the chat list overlay, clearing the search box, cancelling a rename).

export type ShortcutAction =
  | "focus-search"
  | "new-chat"
  | "toggle-chats"
  | "focus-composer"
  | "edit-last"
  | "stop"
  | "show-help"

// Where keyboard focus is: nowhere special, the message box, the chat search box, or some other field.
export type FocusField = "none" | "composer" | "search" | "other"

export type KeyLike = {
  key: string
  ctrlKey: boolean
  metaKey: boolean
  shiftKey: boolean
  altKey: boolean
  defaultPrevented: boolean
  isComposing: boolean
}

export type KeyContext = {
  running: boolean
  overlayOpen: boolean
  dialogOpen: boolean
  field: FocusField
  /** Whether the message box holds no text (the up arrow then edits the last message). */
  composerEmpty: boolean
}

export const SHORTCUTS: readonly { action: ShortcutAction; keys: readonly string[]; label: string }[] = [
  { action: "focus-search", keys: ["Ctrl", "K"], label: "Search chats" },
  { action: "new-chat", keys: ["Ctrl", "N"], label: "New chat" },
  { action: "toggle-chats", keys: ["Ctrl", "B"], label: "Show or hide the chat list" },
  { action: "focus-composer", keys: ["/"], label: "Go to the message box" },
  { action: "edit-last", keys: ["↑"], label: "Edit your last message (in an empty message box)" },
  { action: "stop", keys: ["Esc"], label: "Stop the reply" },
  { action: "show-help", keys: ["?"], label: "Show these shortcuts (also Ctrl+/)" },
]

// Keys the message box itself answers, listed with the shortcuts so they are written down somewhere.
export const COMPOSER_KEYS: readonly { keys: readonly string[]; label: string }[] = [
  { keys: ["Enter"], label: "Send the message" },
  { keys: ["Shift", "Enter"], label: "Start a new line" },
]

export function resolveShortcut(event: KeyLike, context: KeyContext): ShortcutAction | null {
  if (event.defaultPrevented || event.isComposing) return null
  const command = event.ctrlKey || event.metaKey
  const key = event.key.toLowerCase()

  if (command && !event.shiftKey && !event.altKey) {
    if (key === "k") return "focus-search"
    if (key === "n") return "new-chat"
    if (key === "b") return "toggle-chats"
    if (key === "/") return "show-help"
    return null
  }
  if (command || event.altKey) return null

  if (event.key === "Escape") {
    const busyElsewhere = context.dialogOpen || context.overlayOpen || context.field === "search" || context.field === "other"
    return context.running && !busyElsewhere ? "stop" : null
  }
  if (event.key === "?" && context.field === "none" && !context.dialogOpen) return "show-help"
  if (event.key === "/" && !event.shiftKey && context.field === "none" && !context.dialogOpen) return "focus-composer"
  if (
    event.key === "ArrowUp" &&
    !event.shiftKey &&
    context.field === "composer" &&
    context.composerEmpty &&
    !context.running
  ) {
    return "edit-last"
  }
  return null
}

// Classifies the focused element for ``KeyContext.field``. ``composer`` is the message box.
export function fieldOf(element: Element | null): FocusField {
  if (!element) return "none"
  const tag = element.tagName
  if (!(tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (element as HTMLElement).isContentEditable)) {
    return "none"
  }
  if (element.closest("[data-slot='aui_composer-shell']")) return "composer"
  if (element.closest("[data-chat-search]")) return "search"
  return "other"
}
