// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { App } from "@/App"
import { FakeJarvis } from "@/test/fakeJarvis"

// A screen reader hears a reply when it arrives, as a sighted owner sees it land; nothing is read out for the
// history a chat opens with.

const announcements = () => document.querySelector("[data-slot='reply-announcer']")?.textContent ?? ""

describe("new replies", () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it("are read out politely as they arrive, and the history a chat opens with is not", async () => {
    const fake = new FakeJarvis({ chats: [{ id: "c1", title: "Chat", messages: 4 }] })
    vi.stubGlobal("fetch", fake.fetch)
    render(<App />)
    await screen.findByText("Answer 1")
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(announcements()).toBe("")

    await act(async () => fake.voice("what time is it", "It's ten past four."))
    await screen.findByText("It's ten past four.")
    await waitFor(() => expect(announcements()).toBe("Jarvis: It's ten past four."))
    expect(document.querySelector("[data-slot='reply-announcer']")!.getAttribute("aria-live")).toBe("polite")
  })
})
