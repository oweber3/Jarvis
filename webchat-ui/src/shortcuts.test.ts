import { describe, expect, it } from "vitest"
import { SHORTCUTS, resolveShortcut, type KeyContext, type KeyLike } from "@/shortcuts"

const key = (k: string, extra: Partial<KeyLike> = {}): KeyLike => ({
  key: k,
  ctrlKey: false,
  metaKey: false,
  shiftKey: false,
  altKey: false,
  defaultPrevented: false,
  isComposing: false,
  ...extra,
})
const idle: KeyContext = { running: false, overlayOpen: false, dialogOpen: false, field: "none" }

describe("resolveShortcut", () => {
  it("Ctrl+K and Cmd+K focus the search, even while typing a message", () => {
    expect(resolveShortcut(key("k", { ctrlKey: true }), idle)).toBe("focus-search")
    expect(resolveShortcut(key("K", { metaKey: true }), { ...idle, field: "composer" })).toBe("focus-search")
  })

  it("Ctrl+N starts a new chat, also from the message box", () => {
    expect(resolveShortcut(key("n", { ctrlKey: true }), { ...idle, field: "composer" })).toBe("new-chat")
  })

  it("does not take combinations that carry extra modifiers", () => {
    expect(resolveShortcut(key("k", { ctrlKey: true, shiftKey: true }), idle)).toBeNull()
    expect(resolveShortcut(key("n", { ctrlKey: true, altKey: true }), idle)).toBeNull()
  })

  it("leaves plain letters alone, so typing is never captured", () => {
    for (const letter of ["k", "n", "a"]) expect(resolveShortcut(key(letter), idle)).toBeNull()
  })

  it("Esc stops a running reply", () => {
    expect(resolveShortcut(key("Escape"), { ...idle, running: true })).toBe("stop")
  })

  it("Esc does nothing when no reply is running", () => {
    expect(resolveShortcut(key("Escape"), idle)).toBeNull()
  })

  it("Esc keeps its other jobs: dialogs, the narrow sidebar, the search box, other fields, handled keys", () => {
    const running = { ...idle, running: true }
    expect(resolveShortcut(key("Escape"), { ...running, dialogOpen: true })).toBeNull()
    expect(resolveShortcut(key("Escape"), { ...running, overlayOpen: true })).toBeNull()
    expect(resolveShortcut(key("Escape"), { ...running, field: "search" })).toBeNull()
    expect(resolveShortcut(key("Escape"), { ...running, field: "other" })).toBeNull()
    expect(resolveShortcut(key("Escape", { defaultPrevented: true }), running)).toBeNull()
    expect(resolveShortcut(key("Escape", { isComposing: true }), running)).toBeNull()
  })

  it("Esc in the message box stops the reply (the box does it itself, then marks the key handled)", () => {
    expect(resolveShortcut(key("Escape"), { ...idle, running: true, field: "composer" })).toBe("stop")
  })

  it("? opens the shortcut list only outside text fields", () => {
    expect(resolveShortcut(key("?", { shiftKey: true }), idle)).toBe("show-help")
    expect(resolveShortcut(key("?"), { ...idle, field: "composer" })).toBeNull()
    expect(resolveShortcut(key("?"), { ...idle, field: "search" })).toBeNull()
    expect(resolveShortcut(key("?"), { ...idle, dialogOpen: true })).toBeNull()
  })

  it("ignores keys another handler already took", () => {
    expect(resolveShortcut(key("k", { ctrlKey: true, defaultPrevented: true }), idle)).toBeNull()
  })
})

describe("SHORTCUTS", () => {
  it("lists every action once, for the help", () => {
    const actions = SHORTCUTS.map((s) => s.action)
    expect(new Set(actions).size).toBe(actions.length)
    expect(actions).toEqual(expect.arrayContaining(["focus-search", "new-chat", "stop", "show-help"]))
    for (const shortcut of SHORTCUTS) expect(shortcut.keys.length).toBeGreaterThan(0)
  })
})
