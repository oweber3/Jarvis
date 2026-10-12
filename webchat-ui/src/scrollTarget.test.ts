import { describe, expect, it, vi } from "vitest"
import { messageSelector, scrollTarget } from "@/scrollTarget"
import { toThreadMessage } from "@/conversion"

describe("messageSelector", () => {
  it("matches the id the thread gives a stored message", () => {
    const id = toThreadMessage({ id: 42, role: "user", text: "hi", ts: 1, source: "typed" }).id
    expect(messageSelector(42)).toBe(`[data-message-id="${id}"]`)
  })
})

describe("scrollTarget", () => {
  it("tells listeners when a target is set and cleared", () => {
    const seen: (number | null)[] = []
    const stop = scrollTarget.subscribe(() => seen.push(scrollTarget.get()))
    scrollTarget.set(7)
    scrollTarget.set(null)
    stop()
    scrollTarget.set(9)
    expect(seen).toEqual([7, null])
    scrollTarget.set(null)
  })

  it("starts with no target", () => {
    const listener = vi.fn()
    scrollTarget.subscribe(listener)()
    expect(scrollTarget.get()).toBeNull()
  })
})
