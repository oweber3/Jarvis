// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { MarkdownContent } from "@/components/markdown-text"
import { TooltipProvider } from "@/components/ui/tooltip"

// A reply that cannot be drawn shows its words as plain text in its place; the replies around it, and the page, carry
// on. Colouring code is made to fail here to stand for anything that might.

vi.mock("@/highlight", () => ({
  MAX_HIGHLIGHT_CHARS: 20_000,
  highlight: () => {
    throw new Error("cannot colour this")
  },
}))

describe("a reply that cannot be drawn", () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it("shows its words as plain text and leaves the other replies as they are", () => {
    vi.spyOn(console, "error").mockImplementation(() => {})
    const { container } = render(
      <TooltipProvider>
        <MarkdownContent text={"Some words first.\n\n```python\nprint(1)\n```"} />
        <MarkdownContent text="A **bold** reply." />
      </TooltipProvider>,
    )
    const [broken, fine] = [...container.querySelectorAll(".aui-md")]
    expect(broken!.textContent).toContain("Some words first.")
    expect(broken!.textContent).toContain("print(1)")
    expect(fine!.querySelector("strong")!.textContent).toBe("bold")
  })
})
