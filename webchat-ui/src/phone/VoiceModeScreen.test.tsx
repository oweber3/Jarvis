import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"
import type { Confirmation } from "@/api"
import { VoiceModeScreen, type VoiceModeScreenProps } from "@/phone/VoiceModeScreen"
import { voiceMessage } from "@/phone/voiceSession"

const nothing = () => undefined

const confirmation: Confirmation = {
  id: "c1",
  tool: "localFiles",
  action: "Delete a file",
  target: "notes.txt",
  consequence: "It cannot be undone.",
  expires_in: 40,
}

function render(overrides: Partial<VoiceModeScreenProps> = {}): string {
  const props: VoiceModeScreenProps = {
    view: { phase: "listening", message: null, conversation: true },
    orb: { state: "listening", label: null },
    level: () => null,
    exchange: { said: null, reply: null },
    confirmation: null,
    answer: async () => null,
    onProblem: nothing,
    toast: null,
    onDismissToast: nothing,
    onTap: nothing,
    onConversation: nothing,
    onClose: nothing,
    ...overrides,
  }
  return renderToStaticMarkup(<VoiceModeScreen {...props} />)
}

// The opening tag that carries `attribute`.
const tagWith = (html: string, attribute: string) => html.match(new RegExp(`<[^>]*${attribute}[^>]*>`))?.[0] ?? ""

const text = (html: string) => html.replace(/<[^>]+>/g, " ").replace(/&#x27;|&#39;/g, "'").replace(/\s+/g, " ")

describe("the voice mode screen", () => {
  it("has a large close button that ends voice mode", () => {
    expect(render()).toMatch(/<button[^>]*aria-label="End voice mode"/)
  })

  it("shows conversation mode as a switch that is on by default and can be turned off", () => {
    const on = tagWith(render(), 'aria-label="Conversation mode"')
    expect(on).toContain('role="switch"')
    expect(on).toContain('aria-checked="true"')
    expect(tagWith(render({ view: { phase: "idle", message: null, conversation: false } }), 'aria-label="Conversation mode"')).toContain(
      'aria-checked="false"',
    )
  })

  it("lets the owner tap the orb to talk, send or interrupt", () => {
    expect(render({ view: { phase: "idle", message: null, conversation: true } })).toContain('aria-label="Talk to Jarvis"')
    expect(render()).toContain('aria-label="Stop and send"')
    expect(render({ view: { phase: "speaking", message: null, conversation: true } })).toContain('aria-label="Interrupt Jarvis"')
  })

  it("says a refusal in plain words", () => {
    expect(text(render({ view: { phase: "idle", message: voiceMessage("busy"), conversation: false } }))).toContain(voiceMessage("busy"))
  })

  it("shows the latest thing said and Jarvis's reply as short text", () => {
    const shown = text(render({ exchange: { said: "what time is it", reply: "Ten past three." } }))
    expect(shown).toContain("what time is it")
    expect(shown).toContain("Ten past three.")
  })

  it("shows a pending desktop confirmation that can be answered there", () => {
    const html = render({ confirmation })
    expect(text(html)).toContain("Delete a file: notes.txt")
    expect(html).toMatch(/<button[^>]*>Approve<\/button>/)
    expect(html).toMatch(/<button[^>]*>Deny<\/button>/)
  })

  it("shows no confirmation when none waits", () => {
    expect(render()).not.toContain("Approve")
  })

  it("shows a toast inside voice mode", () => {
    expect(text(render({ toast: "That was already answered." }))).toContain("That was already answered.")
  })
})
