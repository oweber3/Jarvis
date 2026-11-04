// @vitest-environment jsdom
import { useAuiState } from "@assistant-ui/react"
import { act, cleanup, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { FC } from "react"
import { Thread } from "@/components/thread.aui"
import { TooltipProvider } from "@/components/ui/tooltip"
import { FakeJarvis } from "@/test/fakeJarvis"
import { JarvisProvider } from "@/useJarvis"

// How much of the thread React redraws when the conversation changes. A chat can hold thousands of turns (every
// voice request lands in the open chat), so one new turn must cost the same in a long chat as in a short one.

const renders = new Map<string, number>()

// Subscribes the way the thread's real parts do: to the message and its parts as objects (the Markdown text reads
// its part), so a turn whose objects are rebuilt counts as redrawn even when its words are the same.
const CountingReply: FC = () => {
  const message = useAuiState((s) => s.message)
  const parts = useAuiState((s) => s.message.parts)
  renders.set(message.id, (renders.get(message.id) ?? 0) + 1)
  return <div data-testid="reply">{parts.map((p) => (p.type === "text" ? p.text : "")).join("")}</div>
}

const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 60)))

function mount(fake: FakeJarvis) {
  vi.stubGlobal("fetch", fake.fetch)
  return render(
    <TooltipProvider>
      <JarvisProvider>
        <Thread components={{ AssistantMessage: CountingReply }} />
      </JarvisProvider>
    </TooltipProvider>,
  )
}

describe("redrawing the thread", () => {
  beforeEach(() => renders.clear())
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it("a new turn in a long chat redraws only the turn and its neighbour, not the whole thread", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Long", messages: 120 }] })
    mount(fake)
    await screen.findByText("Answer 59")
    await settle()
    renders.clear()

    await act(async () => {
      fake.store("c1", "user", "One more question", { quiet: true })
      fake.store("c1", "assistant", "One more answer")
    })
    await screen.findByText("One more answer")
    await settle()

    const redrawn = [...renders.keys()].filter((id) => id !== lastReplyId(fake))
    expect(redrawn.length).toBeLessThanOrEqual(2)
  })

  it("a change of Jarvis's state alone redraws no stored turn", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Long", messages: 40 }] })
    mount(fake)
    await screen.findByText("Answer 19")
    await settle()
    renders.clear()

    await act(async () => {
      fake.busyVoice = true
      fake.bump()
    })
    await settle()
    await act(async () => {
      fake.busyVoice = false
      fake.bump()
    })
    await settle()

    expect([...renders.keys()]).toEqual([])
  })
})

function lastReplyId(fake: FakeJarvis): string {
  const all = fake.messages.get("c1")!
  return `m${all[all.length - 1]!.id}`
}
