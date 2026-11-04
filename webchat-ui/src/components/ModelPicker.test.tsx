// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"
import { ModelPicker } from "@/components/ModelPicker"
import { TooltipProvider } from "@/components/ui/tooltip"
import { FakeJarvis } from "@/test/fakeJarvis"
import { JarvisProvider } from "@/useJarvis"

// The model menu in the message box: the models of who answers now, one of them chosen.

function mount(fake: FakeJarvis) {
  vi.stubGlobal("fetch", fake.fetch)
  return render(
    <TooltipProvider>
      <JarvisProvider>
        <ModelPicker />
      </JarvisProvider>
    </TooltipProvider>,
  )
}

describe("the model menu", () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it("names the model in use and switches to another one", async () => {
    const fake = new FakeJarvis()
    mount(fake)
    const trigger = await screen.findByRole("button", { name: /Model: Gemma 4 12B/ })
    await userEvent.click(trigger)
    const items = await screen.findAllByRole("menuitemradio")
    expect(items.map((i) => i.textContent)).toEqual([
      expect.stringContaining("Gemma 4 12B"),
      expect.stringContaining("Qwen 3.5 9B"),
      expect.stringContaining("Huge 70B"),
    ])
    expect(items[0]!.getAttribute("aria-checked")).toBe("true")
    await act(async () => fireEvent.click(items[1]!))
    await waitFor(() => expect(fake.modelChoices).toContainEqual({ kind: "local", value: "qwen" }))
  })

  it("shows a model that is not installed as such and does not offer it", async () => {
    const fake = new FakeJarvis()
    mount(fake)
    await userEvent.click(await screen.findByRole("button", { name: /Model:/ }))
    const huge = (await screen.findAllByRole("menuitemradio")).find((i) => i.textContent?.includes("Huge 70B"))!
    expect(huge.textContent).toContain("Not installed")
    expect(huge.getAttribute("aria-disabled")).toBe("true")
  })
})
