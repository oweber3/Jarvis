import type { Chat, SearchHit } from "@/api"
import { chatTitle } from "@/conversion"

// What the sidebar shows while the owner searches: chats whose title matches (instantly, from the library)
// together with chats whose messages match (from the server, with a snippet). Pure, covered by tests.

export type SearchRow = { chat: Chat; messageId: number | null; snippet: string | null }

export function searchRows(chats: readonly Chat[], query: string, hits: readonly SearchHit[]): SearchRow[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return []
  const byChat = new Map(hits.map((h) => [h.chat_id, h]))
  const rows: SearchRow[] = []
  for (const chat of chats) {
    const found = byChat.get(chat.id)
    const titled = chatTitle(chat).toLowerCase().includes(needle)
    if (!found && !titled) continue
    rows.push({ chat, messageId: found?.message_id ?? null, snippet: found?.snippet ?? null })
  }
  return rows
}

// Splits ``text`` around case-insensitive occurrences of ``query`` (plain text, no patterns).
export function highlightParts(text: string, query: string): { text: string; match: boolean }[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return [{ text, match: false }]
  const lower = text.toLowerCase()
  const parts: { text: string; match: boolean }[] = []
  let from = 0
  for (;;) {
    const at = lower.indexOf(needle, from)
    if (at < 0) break
    if (at > from) parts.push({ text: text.slice(from, at), match: false })
    parts.push({ text: text.slice(at, at + needle.length), match: true })
    from = at + needle.length
  }
  if (from < text.length) parts.push({ text: text.slice(from), match: false })
  return parts.length ? parts : [{ text, match: false }]
}
