// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import type { ToolStep } from "@/api"
import { Thread } from "@/components/thread.aui"
import { TooltipProvider } from "@/components/ui/tooltip"
import { FakeJarvis } from "@/test/fakeJarvis"
import { JarvisProvider } from "@/useJarvis"

// What Jarvis did for a reply, and what it is doing while the owner waits.

function mount(fake: FakeJarvis) {
  vi.stubGlobal("fetch", fake.fetch)
  return render(
    <TooltipProvider>
      <JarvisProvider>
        <Thread />
      </JarvisProvider>
    </TooltipProvider>,
  )
}

const step = (label: string, status: ToolStep["status"] = "done", summary: string | null = null): ToolStep => ({
  tool: "someTool",
  status,
  label,
  summary,
})

describe("tool steps", () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it("shows the steps taken so far under Thinking while Jarvis works", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Chat", messages: 2 }] })
    fake.next = "hold"
    mount(fake)
    await screen.findByText("Answer 0")
    const box = screen.getByRole("textbox", { name: "Message input" })
    await act(async () => fireEvent.change(box, { target: { value: "open word and look it up" } }))
    fake.liveSteps = [{ tool: "openApp", status: "done", label: "Opened Word" }]
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Send message" })))
    await screen.findByText("Opened Word")
    expect(screen.getByText("Thinking")).toBeTruthy()

    await act(async () => fake.finish("Word is open.", [step("Opened Word", "done", "Word")]))
    await screen.findByText("Word is open.")
    expect(screen.queryByText("Thinking")).toBeNull()
    expect(screen.getByText(/· Word/)).toBeTruthy()
  })

  it("says beside a step that it waits for the owner, without opening anything", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Chat", messages: 0 }] })
    fake.store("c1", "user", "delete the old folder", { quiet: true })
    fake.store("c1", "assistant", "Please confirm on your desktop.", { steps: [step("Local files", "waiting")], quiet: true })
    mount(fake)
    await screen.findByText("Please confirm on your desktop.")
    expect(screen.getByText(/Waiting for your confirmation/)).toBeTruthy()
  })

  it("folds many steps into one line that opens to them", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Chat", messages: 0 }] })
    const steps = [step("Opened Word"), step("Searched the web"), step("Read a page"), step("Saved a note", "failed")]
    fake.store("c1", "user", "do the lot", { quiet: true })
    fake.store("c1", "assistant", "All done but the note.", { steps, quiet: true })
    mount(fake)
    await screen.findByText("All done but the note.")
    const fold = screen.getByRole("button", { name: /4 steps · 1 didn't work/ })
    expect(fold.getAttribute("aria-expanded")).toBe("false")
    expect(screen.getByText("Opened Word").closest("[hidden]")).not.toBeNull()
    await act(async () => fireEvent.click(fold))
    expect(fold.getAttribute("aria-expanded")).toBe("true")
    expect(screen.getByText("Opened Word").closest("[hidden]")).toBeNull()
  })
})
