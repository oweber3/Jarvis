import { describe, expect, it, vi } from "vitest"
import { greetingFor, partOfDay, runCard, sanitiseCards, type Card } from "@/startScreen"

const at = (hour: number) => new Date(2026, 9, 12, hour, 30)

describe("start screen greeting", () => {
  it("follows the hour of the day", () => {
    expect(partOfDay(at(5))).toBe("morning")
    expect(partOfDay(at(11))).toBe("morning")
    expect(partOfDay(at(12))).toBe("afternoon")
    expect(partOfDay(at(17))).toBe("afternoon")
    expect(partOfDay(at(18))).toBe("evening")
    expect(partOfDay(at(23))).toBe("evening")
    expect(partOfDay(at(2))).toBe("evening")
  })

  it("is a short greeting for each part of the day", () => {
    const words = new Set([at(8), at(13), at(20)].map(greetingFor))
    expect(words.size).toBe(3)
  })
})

describe("start screen cards", () => {
  it("sends a card marked send and fills the composer for anything else", () => {
    const send = vi.fn()
    const fill = vi.fn()
    runCard({ kind: "routine", text: "go", action: "send" }, { send, fill })
    runCard({ kind: "example", text: "ask", action: "fill" }, { send, fill })
    runCard({ kind: "example", text: "odd", action: "other" as Card["action"] }, { send, fill })
    expect(send.mock.calls).toEqual([["go"]])
    expect(fill.mock.calls).toEqual([["ask"], ["odd"]])
  })

  it("keeps well-formed cards only", () => {
    const cards = sanitiseCards({
      cards: [
        { kind: "routine", text: "Run my a routine", action: "send" },
        { kind: "example", text: "", action: "fill" },
        { kind: "example", action: "fill" },
        "nope",
        { kind: "example", text: "ok", action: "fill" },
      ],
    })
    expect(cards.map((c) => c.text)).toEqual(["Run my a routine", "ok"])
  })

  it("copes with a reply that is not a card list", () => {
    expect(sanitiseCards(null)).toEqual([])
    expect(sanitiseCards({})).toEqual([])
    expect(sanitiseCards({ cards: "x" })).toEqual([])
  })
})
