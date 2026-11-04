// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { App } from "@/App"
import { FakeJarvis } from "@/test/fakeJarvis"

// The whole page against an in-memory Jarvis: the header's presence line and the keyboard.

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

  it("the up arrow in an empty message box opens the last message sent for editing", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Chat", messages: 4 }] })
    mount(fake)
    await screen.findByText("Answer 1")
    messageBox().focus()
    await act(async () => fireEvent.keyDown(messageBox(), { key: "ArrowUp" }))
    const editing = (await screen.findByRole("textbox", { name: "Edit message" })) as HTMLTextAreaElement
    expect(editing.value).toBe("Question 1")
  })

  it("the up arrow edits nothing while the page shows an earlier stretch of the chat", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Long", messages: 400 }] })
    mount(fake)
    await screen.findByText("Answer 199")
    // Reading far enough back, the page lets go of the newest messages.
    for (let i = 0; i < 10 && !screen.queryByRole("button", { name: "Back to the latest" }); i++) {
      await act(async () => fireEvent.click(await screen.findByRole("button", { name: "Load earlier messages" })))
      await screen.findAllByRole("button", { name: /^Load earlier messages$|^Back to the latest$/ })
    }
    expect(screen.getByRole("button", { name: "Back to the latest" })).toBeTruthy()

    const uncaught: unknown[] = []
    const onError = (event: ErrorEvent) => {
      uncaught.push(event.error)
      event.preventDefault()
    }
    window.addEventListener("error", onError)
    messageBox().focus()
    const key = new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true, cancelable: true })
    await act(async () => messageBox().dispatchEvent(key))
    window.removeEventListener("error", onError)
    expect(screen.queryByRole("textbox", { name: "Edit message" })).toBeNull()
    expect(uncaught).toEqual([])
    expect(key.defaultPrevented).toBe(false)
  }, 60_000)

  it("Ctrl+B hides and shows the chat list", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Chat", messages: 2 }] })
    mount(fake)
    await screen.findByText("Answer 0")
    const list = () => screen.getByRole("complementary", { name: "Chats" })
    const shown = list().className.includes("hidden") === false
    await act(async () => fireEvent.keyDown(window, { key: "b", ctrlKey: true }))
    expect(list().className.includes("hidden")).toBe(shown)
    await act(async () => fireEvent.keyDown(window, { key: "b", ctrlKey: true }))
    expect(list().className.includes("hidden")).toBe(!shown)
  })

  it("lists the keys of the message box with the shortcuts", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Chat", messages: 2 }] })
    mount(fake)
    await screen.findByText("Answer 0")
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Keyboard shortcuts" })))
    expect(await screen.findByText("Start a new line")).toBeTruthy()
    expect(screen.getByText("Edit your last message (in an empty message box)")).toBeTruthy()
  })
})
