import type { ThreadMessageLike } from "@assistant-ui/react"
import type { Chat, ChatMessage, Notice } from "@/api"

// Turns what the server stores into messages the thread renders. Pure functions, covered by tests.

// A message being sent: shown at once with a thinking placeholder. ``afterId`` is the newest stored message
// when it was sent, so a reply stored since then can be told from older turns.
export type Pending = { text: string; accepted: boolean; afterId: number }

export function toThreadMessage(message: ChatMessage): ThreadMessageLike {
  return {
    id: `m${message.id}`,
    role: message.role,
    content: [{ type: "text", text: message.text }],
    createdAt: new Date(message.ts * 1000),
    ...(message.role === "assistant" ? { status: { type: "complete", reason: "stop" } as const } : {}),
    metadata: {
      ...(message.source === "voice" ? { modality: "voice" as const } : {}),
      custom: { source: message.source },
    },
  }
}

// A local line (busy, stopped, could not answer): shown, never stored. A failed request is shown
// first as the message the owner sent.
export function noticeMessages(notice: Notice): ThreadMessageLike[] {
  const out: ThreadMessageLike[] = []
  const createdAt = new Date(notice.ts * 1000)
  if (notice.request) {
    out.push({
      id: `n${notice.id}-request`,
      role: "user",
      content: [{ type: "text", text: notice.request }],
      createdAt,
    })
  }
  out.push({
    id: `n${notice.id}`,
    role: "assistant",
    content: [{ type: "text", text: notice.text }],
    createdAt,
    status: { type: "complete", reason: "stop" },
    metadata: { custom: { notice: true, kind: notice.kind } },
  })
  return out
}

export function buildThreadMessages(
  messages: readonly ChatMessage[],
  notices: readonly Notice[],
  pending: Pending | null,
  now: () => Date = () => new Date(),
): ThreadMessageLike[] {
  const out = messages.map(toThreadMessage)
  for (const notice of notices) out.push(...noticeMessages(notice))
  if (pending) {
    const createdAt = now()
    out.push({ id: "pending-user", role: "user", content: [{ type: "text", text: pending.text }], createdAt })
    out.push({
      id: "pending-reply",
      role: "assistant",
      content: [],
      createdAt,
      status: { type: "running" },
    })
  }
  return out
}

export function chatTitle(chat: Pick<Chat, "title">): string {
  return chat.title.trim() || "New chat"
}

// Messages of the open chat as the long poll delivers them: append what is new, never twice.
export function mergeMessages(current: readonly ChatMessage[], incoming: readonly ChatMessage[]): ChatMessage[] {
  if (incoming.length === 0) return current as ChatMessage[]
  const seen = new Set(current.map((m) => m.id))
  const fresh = incoming.filter((m) => !seen.has(m.id))
  return fresh.length === 0 ? (current as ChatMessage[]) : [...current, ...fresh]
}

export function lastMessageId(messages: readonly ChatMessage[]): number {
  return messages.reduce((highest, m) => Math.max(highest, m.id), 0)
}

// The placeholder goes once Jarvis is no longer working on the request and either the send was confirmed
// or a reply newer than the message is already stored (a fast answer can land before the confirmation).
export function pendingDone(pending: Pending | null, busyQuery: boolean, messages: readonly ChatMessage[]): boolean {
  if (!pending || busyQuery) return false
  return pending.accepted || messages.some((m) => m.role === "assistant" && m.id > pending.afterId)
}
