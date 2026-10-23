import { describe, expect, it } from "vitest"
import { KeyboardTracker } from "@/phone/keyboard"

describe("the on-screen keyboard", () => {
  it("is closed when the visual viewport is as tall as the page", () => {
    expect(new KeyboardTracker().update(800, 800, 390)).toBe(false)
  })

  it("is open when only the visual viewport shrank (iOS)", () => {
    const tracker = new KeyboardTracker()
    tracker.update(800, 800, 390)
    expect(tracker.update(800, 480, 390)).toBe(true)
  })

  it("is open when the layout shrank with it (Android)", () => {
    const tracker = new KeyboardTracker()
    tracker.update(800, 800, 390)
    expect(tracker.update(470, 470, 390)).toBe(true)
    expect(tracker.update(800, 800, 390)).toBe(false)
  })

  it("ignores the small change of a browser bar", () => {
    const tracker = new KeyboardTracker()
    tracker.update(800, 800, 390)
    expect(tracker.update(740, 740, 390)).toBe(false)
  })

  it("starts again when the phone is turned", () => {
    const tracker = new KeyboardTracker()
    tracker.update(800, 800, 390)
    expect(tracker.update(390, 390, 800)).toBe(false)
  })
})
