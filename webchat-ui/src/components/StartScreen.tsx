import { useAui, useAuiState } from "@assistant-ui/react"
import { LayoutGridIcon, MessageSquareIcon, WorkflowIcon } from "lucide-react"
import { useEffect, useState, type FC } from "react"
import { api } from "@/api"
import { JarvisMark } from "@/components/JarvisMark"
import { greetingFor, runCard, sanitiseCards, type Card } from "@/startScreen"

// What an empty chat shows: a greeting and a few things to try. The server builds the cards from the
// owner's routines and workspaces (names only) and some generic examples; no model is asked.

const ICONS: Record<Card["kind"], FC<{ className?: string }>> = {
  routine: WorkflowIcon,
  workspace: LayoutGridIcon,
  example: MessageSquareIcon,
}

// `limit` caps the cards shown (the phone has room for fewer).
export const StartScreen: FC<{ limit?: number }> = ({ limit = Infinity }) => {
  const aui = useAui()
  const unavailable = useAuiState((s) => s.thread.isDisabled || s.thread.isRunning)
  const [cards, setCards] = useState<Card[]>([])
  const greeting = greetingFor(new Date())

  useEffect(() => {
    let current = true
    api
      .suggestions()
      .then((reply) => current && setCards(sanitiseCards(reply)))
      .catch(() => current && setCards([]))
    return () => {
      current = false
    }
  }, [])

  const choose = (card: Card) =>
    runCard(card, {
      send: (text) => aui.thread().append({ role: "user", content: [{ type: "text", text }] }),
      fill: (text) => {
        aui.composer().setText(text)
        document.querySelector<HTMLTextAreaElement>(".aui-composer-root textarea")?.focus()
      },
    })

  return (
    <div className="aui-thread-welcome-root mb-6 flex flex-col gap-5 px-2">
      <div className="fade-in slide-in-from-bottom-1 animate-in fill-mode-both flex items-center gap-3 duration-200">
        <JarvisMark className="size-11" />
        <div className="flex flex-col">
          <p className="text-2xl font-semibold tracking-tight">{greeting}</p>
          <p className="text-muted-foreground text-sm">Ask anything, or start with one of these.</p>
        </div>
      </div>
      {cards.length > 0 && (
        <ul aria-label="Suggestions" className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {cards.slice(0, limit).map((card) => {
            const Icon = ICONS[card.kind] ?? MessageSquareIcon
            const blocked = unavailable && card.action === "send"
            return (
              <li key={`${card.kind}:${card.text}`}>
                <button
                  type="button"
                  disabled={blocked}
                  onClick={() => choose(card)}
                  className="border-border bg-card/70 hover:border-primary/40 hover:bg-accent hover:text-accent-foreground focus-visible:ring-ring group flex h-full min-h-14 w-full items-center gap-3 rounded-xl border px-4 py-3 text-start text-sm transition-colors focus-visible:ring-2 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <span className="bg-muted text-primary flex size-8 shrink-0 items-center justify-center rounded-lg">
                    <Icon aria-hidden className="size-4" />
                  </span>
                  <span className="min-w-0 break-words">{card.text}</span>
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
