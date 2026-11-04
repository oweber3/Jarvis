// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { Thread } from "@/components/thread.aui"
import { TooltipProvider } from "@/components/ui/tooltip"
import { FakeJarvis } from "@/test/fakeJarvis"
import { JarvisProvider } from "@/useJarvis"

// Stop and busy, as the desktop chat has them: Stop gives the message box back at once and the owner's words stay in
// the thread; a message Jarvis was too busy to take is not lost.

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

const box = () => screen.getByRole("textbox", { name: "Message input" }) as HTMLTextAreaElement

async function send(text: string) {
  await act(async () => fireEvent.change(box(), { target: { value: text } }))
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Send message" })))
}

describe("stopping and busy", () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it("Stop gives the message box back at once and keeps the owner's words in the thread", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Chat", messages: 2 }] })
    fake.next = "hold"
    mount(fake)
    await screen.findByText("Answer 0")
    await send("a slow question")
    await screen.findByRole("button", { name: "Stop generating" })

    // the server has not answered the stop yet
    const finish = fake.finish.bind(fake)
    fake.finish = () => undefined
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Stop generating" })))
    expect(screen.getByRole("button", { name: "Send message" })).toBeTruthy()
    expect(screen.queryByLabelText("Assistant is working")).toBeNull()
    expect(screen.getByText("a slow question")).toBeTruthy()

    await act(async () => finish(null))
    await screen.findByText("Stopped.")
    expect(screen.getAllByText("a slow question")).toHaveLength(1)
  })

  it("a message Jarvis was too busy to take goes back into the message box", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Chat", messages: 2 }] })
    mount(fake)
    await screen.findByText("Answer 0")
    fake.busyVoice = true
    await send("are you there?")
    await waitFor(() => expect(box().value).toBe("are you there?"))
    await screen.findByText("Jarvis is busy with another request.")
  })
})
