// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { Thread } from "@/components/thread.aui"
import { TooltipProvider } from "@/components/ui/tooltip"
import { FakeJarvis } from "@/test/fakeJarvis"
import { JarvisProvider } from "@/useJarvis"

// Edit and Ask again: a sent message is asked again (as it was, or reworded) and what followed it is replaced, as
// the desktop chat's Rewind does. The server takes the turns back; the page shows the new exchange in their place.

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

const messageWith = (text: string) => screen.getByText(text).closest("[data-message-id]") as HTMLElement

describe("asking a sent message again", () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it("edits a sent message and replaces it and what followed with the new exchange", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Chat", messages: 4 }] })
    fake.replyText = "A new answer."
    mount(fake)
    await screen.findByText("Answer 1")

    await act(async () => fireEvent.click(within(messageWith("Question 0")).getByRole("button", { name: "Edit" })))
    const box = screen.getByRole("textbox", { name: "Edit message" }) as HTMLTextAreaElement
    expect(box.value).toBe("Question 0")
    expect(screen.getByText(/forgets the 3 messages after this one/)).toBeTruthy()
    await act(async () => fireEvent.change(box, { target: { value: "Question zero, reworded" } }))
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Send" })))

    await screen.findByText("A new answer.")
    expect(fake.rewinds.at(-1)).toMatchObject({ text: "Question zero, reworded" })
    expect(screen.queryByText("Answer 0")).toBeNull()
    expect(screen.queryByText("Question 1")).toBeNull()
    expect(screen.getByText("Question zero, reworded")).toBeTruthy()
  })

  it("asks the latest message again as it was", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Chat", messages: 4 }] })
    fake.replyText = "Another answer."
    mount(fake)
    await screen.findByText("Answer 1")

    await act(async () => fireEvent.click(within(messageWith("Answer 1")).getByRole("button", { name: "Ask again" })))
    await screen.findByText("Another answer.")
    expect(fake.rewinds.at(-1)!.text).toBeUndefined()
    expect(screen.queryByText("Answer 1")).toBeNull()
    expect(screen.getByText("Question 1")).toBeTruthy()
  })

  it("offers Ask again only on the latest reply", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Chat", messages: 4 }] })
    mount(fake)
    await screen.findByText("Answer 1")
    expect(within(messageWith("Answer 0")).queryByRole("button", { name: "Ask again" })).toBeNull()
    expect(within(messageWith("Answer 1")).getByRole("button", { name: "Ask again" })).toBeTruthy()
  })

  it("puts the messages back and says why when Jarvis cannot ask again", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Chat", messages: 4 }] })
    mount(fake)
    await screen.findByText("Answer 1")
    fake.busyVoice = true

    await act(async () => fireEvent.click(within(messageWith("Answer 1")).getByRole("button", { name: "Ask again" })))
    await waitFor(() => expect(fake.rewinds).toHaveLength(1))
    await screen.findByText("Answer 1")
    expect(screen.getByText("Question 1")).toBeTruthy()
  })

  it("sends an edited message that was never answered as a new message", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Chat", messages: 2 }] })
    mount(fake)
    await screen.findByText("Answer 0")
    await act(async () => fake.notice("failed", "Jarvis couldn't answer that.", "Do the thing"))
    await screen.findByText("Do the thing")

    await act(async () => fireEvent.click(within(messageWith("Do the thing")).getByRole("button", { name: "Edit" })))
    const box = screen.getByRole("textbox", { name: "Edit message" })
    await act(async () => fireEvent.change(box, { target: { value: "Do the other thing" } }))
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Send" })))

    await waitFor(() => expect(fake.sent).toContain("Do the other thing"))
    expect(fake.rewinds).toEqual([])
  })
})
