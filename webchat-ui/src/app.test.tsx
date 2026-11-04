// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { App } from "@/App"
import { FakeJarvis } from "@/test/fakeJarvis"

// The whole page against an in-memory Jarvis: the header's presence line .

function mount(fake: FakeJarvis) {
  vi.stubGlobal("fetch", fake.fetch)
  return render(<App />)
}

const presence = () => document.querySelector("[data-slot='presence']") as HTMLElement
const messageBox = () => screen.getByRole("textbox", { name: "Message input" }) as HTMLTextAreaElement

describe("the page", () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it("says in the header whether Jarvis is online and when it is thinking", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Chat", messages: 2 }] })
    fake.next = "hold"
    mount(fake)
    await screen.findByText("Answer 0")
    await waitFor(() => expect(presence().textContent).toBe("Online"))

    await act(async () => fireEvent.change(messageBox(), { target: { value: "think hard" } }))
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Send message" })))
    await waitFor(() => expect(presence().textContent).toBe("Thinking"))
    await act(async () => fake.finish("Done thinking."))
    await waitFor(() => expect(presence().textContent).toBe("Online"))
  })

  it("reads Offline when Jarvis cannot be reached", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Chat", messages: 2 }] })
    mount(fake)
    await screen.findByText("Answer 0")
    vi.stubGlobal("fetch", () => Promise.reject(new TypeError("offline")))
    await act(async () => fake.bump())
    await waitFor(() => expect(presence().textContent).toBe("Offline"))
  })
})
