import { useAuiState } from "@assistant-ui/react"
import { useEffect, useRef, useState, type FC } from "react"
import { useJarvis } from "@/useJarvis"

// What a screen reader hears when a reply lands, as a sighted owner sees it arrive. The history a chat opens with, and
// earlier pages, are not read out. Polite: it waits for what is being read to finish.

const MAX_CHARS = 400

export const ReplyAnnouncer: FC = () => {
  const { chat } = useJarvis()
  const loading = useAuiState((s) => s.thread.isLoading)
  const lastId = useAuiState((s) => {
    const last = s.thread.messages[s.thread.messages.length - 1]
    return last && last.role === "assistant" && last.status?.type !== "running" ? last.id : null
  })
  const lastText = useAuiState((s) => {
    const last = s.thread.messages[s.thread.messages.length - 1]
    if (!last || last.role !== "assistant") return ""
    return last.parts
      .map((part) => (part.type === "text" ? part.text : ""))
      .join(" ")
      .trim()
  })
  const isNotice = useAuiState((s) => s.thread.messages[s.thread.messages.length - 1]?.metadata.custom?.["notice"] === true)
  const known = useRef<string | null | undefined>(undefined)
  const [said, setSaid] = useState("")

  // A chat that opens starts from what it already holds.
  const chatId = chat?.id
  useEffect(() => {
    known.current = undefined
    setSaid("")
  }, [chatId])

  useEffect(() => {
    if (loading) return
    if (known.current === undefined) {
      known.current = lastId
      return
    }
    if (lastId === null || lastId === known.current || !lastText) return
    known.current = lastId
    const text = lastText.length > MAX_CHARS ? `${lastText.slice(0, MAX_CHARS - 1)}…` : lastText
    setSaid(isNotice ? text : `Jarvis: ${text}`)
  }, [isNotice, lastId, lastText, loading])

  return (
    <div data-slot="reply-announcer" aria-live="polite" aria-atomic="true" className="sr-only">
      {said}
    </div>
  )
}
