import { describe, expect, it } from "vitest"
import { isNarrow, NARROW_QUERY } from "@/layout"

const windowOf = (matches: (query: string) => boolean) => ({ matchMedia: (query: string) => ({ matches: matches(query) }) as MediaQueryList })

describe("isNarrow", () => {
  it("asks the browser whether the window is narrow", () => {
    expect(isNarrow(windowOf((query) => query === NARROW_QUERY))).toBe(true)
    expect(isNarrow(windowOf(() => false))).toBe(false)
  })
})
