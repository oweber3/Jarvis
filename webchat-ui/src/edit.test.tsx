// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import type { ReactNode } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { Thread } from "@/components/thread.aui"
import { TooltipProvider } from "@/components/ui/tooltip"
import { FakeJarvis } from "@/test/fakeJarvis"
import { JarvisProvider, useJarvis } from "@/useJarvis"

// Edit and Ask again: a sent message is asked again (as it was, or reworded) and what followed it is replaced, as
// the desktop chat's Rewind does. The server takes the turns back; the page shows the new exchange in their place.

function mount(fake: FakeJarvis, extra?: ReactNode) {
  vi.stubGlobal("fetch", fake.fetch)
  return render(
    <TooltipProvider>
      <JarvisProvider>
        {extra}
        <Thread />
      </JarvisProvider>
    </TooltipProvider>,
  )
}

// Opens the chat around an earlier message, as a search result does.
function OpenEarlier({ messageId }: { messageId: number }) {
  const { actions } = useJarvis()
  return (
    <button type="button" onClick={() => void actions.openChat("c1", messageId)}>
      Open earlier
    </button>
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

  it("offers neither Edit nor Ask again while the page shows an earlier stretch of the chat", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Long", messages: 400 }] })
    mount(fake, <OpenEarlier messageId={42} />)
    await screen.findByText("Answer 199")
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Open earlier" })))
    await screen.findByRole("button", { name: "Back to the latest" })
    expect(screen.getByText("Answer 20")).toBeTruthy()
    expect(screen.queryAllByRole("button", { name: "Edit" })).toEqual([])
    expect(screen.queryAllByRole("button", { name: "Ask again" })).toEqual([])

    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Back to the latest" })))
    await screen.findByText("Answer 199")
    expect(screen.getAllByRole("button", { name: "Edit" }).length).toBeGreaterThan(0)
    expect(within(messageWith("Answer 199")).getByRole("button", { name: "Ask again" })).toBeTruthy()
  })

  it("names the newest message it shows, so the server takes back nothing the page has not shown", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Chat", messages: 4 }] })
    mount(fake)
    await screen.findByText("Answer 1")
    await act(async () => fireEvent.click(within(messageWith("Answer 1")).getByRole("button", { name: "Ask again" })))
    await waitFor(() => expect(fake.rewinds).toHaveLength(1))
    expect(fake.rewinds[0]!.newest_id).toBe(4)
  })

  it("changes nothing when the chat gained a message the page had not shown, and then shows it", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Chat", messages: 4 }] })
    mount(fake)
    await screen.findByText("Answer 1")
    fake.store("c1", "user", "Said meanwhile", { source: "voice", quiet: true })

    await act(async () => fireEvent.click(within(messageWith("Answer 1")).getByRole("button", { name: "Ask again" })))
    await waitFor(() => expect(fake.rewinds).toHaveLength(1))
    await screen.findByText("Said meanwhile")
    expect(screen.getByText("Answer 1")).toBeTruthy()
    expect(fake.messages.get("c1")!.map((m) => m.text)).toEqual(["Question 0", "Answer 0", "Question 1", "Answer 1", "Said meanwhile"])
  })

  it("counts only the stored messages an edit takes back", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Chat", messages: 4 }] })
    mount(fake)
    await screen.findByText("Answer 1")
    await act(async () => fake.notice("busy", "I was busy with something else.", null))
    await screen.findByText("I was busy with something else.")

    await act(async () => fireEvent.click(within(messageWith("Question 0")).getByRole("button", { name: "Edit" })))
    expect(screen.getByText(/forgets the 3 messages after this one/)).toBeTruthy()
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
