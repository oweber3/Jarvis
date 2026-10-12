// The start screen of an empty chat: a greeting for the time of day and the cards the server offers
// (jarvis/webchat/suggestions.py). Interface wording lives here; the cards' own text comes from the server.

export type PartOfDay = "morning" | "afternoon" | "evening"

export type Card = { kind: "routine" | "workspace" | "example"; text: string; action: "send" | "fill" }

const GREETINGS: Record<PartOfDay, string> = {
  morning: "Good morning",
  afternoon: "Good afternoon",
  evening: "Good evening",
}

export function partOfDay(now: Date): PartOfDay {
  const hour = now.getHours()
  if (hour >= 5 && hour < 12) return "morning"
  if (hour >= 12 && hour < 18) return "afternoon"
  return "evening"
}

export function greetingFor(now: Date): string {
  return GREETINGS[partOfDay(now)]
}

/** Complete commands are sent as they stand; anything else only fills the composer for the owner to adjust. */
export function runCard(card: Card, actions: { send: (text: string) => void; fill: (text: string) => void }): void {
  if (card.action === "send") actions.send(card.text)
  else actions.fill(card.text)
}

/** The well-formed cards of a server reply; anything else is ignored. */
export function sanitiseCards(reply: unknown): Card[] {
  const cards = (reply as { cards?: unknown } | null)?.cards
  if (!Array.isArray(cards)) return []
  return cards.filter(
    (card): card is Card =>
      typeof card === "object" &&
      card !== null &&
      typeof (card as Card).text === "string" &&
      (card as Card).text.trim() !== "" &&
      typeof (card as Card).kind === "string" &&
      typeof (card as Card).action === "string",
  )
}
