// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { ErrorBoundary } from "@/components/ErrorBoundary"

const Broken = () => {
  throw new Error("boom")
}

describe("a part of the page that breaks", () => {
  afterEach(cleanup)

  it("is replaced by a plain explanation and a way to reload, never a blank page", () => {
    const quiet = vi.spyOn(console, "error").mockImplementation(() => undefined)
    render(
      <ErrorBoundary>
        <Broken />
      </ErrorBoundary>,
    )
    expect(screen.getByRole("alert").textContent).toContain("The chat page hit a problem")
    expect(screen.getByRole("button", { name: "Reload the page" })).toBeTruthy()
    quiet.mockRestore()
  })
})
