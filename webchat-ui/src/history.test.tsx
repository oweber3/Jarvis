// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { Thread } from "@/components/thread.aui"
import { TooltipProvider } from "@/components/ui/tooltip"
import { PAGE_MESSAGES } from "@/sync"
import { FakeJarvis } from "@/test/fakeJarvis"
import { JarvisProvider } from "@/useJarvis"

// A chat is opened on its newest messages; earlier ones come a page at a time. Every voice request lands in the
// open chat, so the chat Jarvis opens first can hold thousands of turns.

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

describe("a long chat", () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it("opens on its newest messages and fetches no more than a page", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Long", messages: 400 }] })
    mount(fake)
    await screen.findByText("Answer 199")
    expect(screen.queryByText("Question 0")).toBeNull()
    expect(document.querySelectorAll("[data-message-id]").length).toBeLessThanOrEqual(PAGE_MESSAGES)
    const loads = fake.requests.filter((r) => r.path.startsWith("/api/chats/c1"))
    expect(loads.map((r) => r.path)).toEqual([`/api/chats/c1?limit=${PAGE_MESSAGES}`])
  })

  it("puts earlier messages in front when asked", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Long", messages: 400 }] })
    mount(fake)
    await screen.findByText("Answer 199")
    await act(async () => fireEvent.click(await screen.findByRole("button", { name: "Load earlier messages" })))
    await screen.findByText(`Question ${200 - PAGE_MESSAGES}`)
    expect(screen.getByText("Answer 199")).toBeTruthy()
  })
})
