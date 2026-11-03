import { describe, expect, it } from "vitest"
import { deadlineFor, secondsLeft } from "@/phone/countdown"

describe("the confirmation countdown", () => {
  it("counts down from what the PC reported", () => {
    const deadline = deadlineFor(41, 1000)
    expect(secondsLeft(deadline, 1000)).toBe(41)
    expect(secondsLeft(deadline, 1000 + 10_000)).toBe(31)
  })

  it("rounds up so it never reads zero while time is left", () => {
    expect(secondsLeft(deadlineFor(2, 0), 1900)).toBe(1)
  })

  it("stops at zero", () => {
    expect(secondsLeft(deadlineFor(2, 0), 9000)).toBe(0)
  })

  it("treats a negative report as already over", () => {
    expect(secondsLeft(deadlineFor(-5, 0), 0)).toBe(0)
  })
})
