import { describe, expect, it } from "vitest"
import { CONFIRM_WORD, confirmWordTyped } from "@/confirm"

describe("confirmWordTyped", () => {
  it("accepts the word however it is cased or padded", () => {
    expect(confirmWordTyped(CONFIRM_WORD)).toBe(true)
    expect(confirmWordTyped(`  ${CONFIRM_WORD.toUpperCase()} `)).toBe(true)
  })

  it("rejects anything else, including part of the word and an empty box", () => {
    expect(confirmWordTyped("")).toBe(false)
    expect(confirmWordTyped(CONFIRM_WORD.slice(0, -1))).toBe(false)
    expect(confirmWordTyped(`${CONFIRM_WORD} all`)).toBe(false)
  })

  it("compares with the word it is given", () => {
    expect(confirmWordTyped("erase", "erase")).toBe(true)
    expect(confirmWordTyped(CONFIRM_WORD, "erase")).toBe(false)
  })
})
