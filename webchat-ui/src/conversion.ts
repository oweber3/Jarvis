import type { ThreadMessageLike } from "@assistant-ui/react"
import type { AttachmentMarker, Chat, ChatMessage, Notice } from "@/api"
import { chipType, markerLabel } from "@/attachments"
import type { LiveStep } from "@/steps"
type ThreadMessageLikePart = Exclude<ThreadMessageLike["content"], string>[number]

// Turns what the server stores into messages the thread renders. Pure functions, covered by tests.

// A message being sent: shown at once with a thinking placeholder. ``afterId`` is the newest stored message
// when it was sent, so a reply stored since then can be told from older turns. ``acceptedAt`` is how many
// polls had been made when the server accepted it (0 until then): only a later poll can know about it.
// ``stopped`` is set once the owner pressed Stop: the words stay, Jarvis is no longer shown working on them.
export type Pending = {
  text: string
  accepted: boolean
  afterId: number
  acceptedAt: number
  attachments?: AttachmentMarker[]
  stopped?: boolean
}

// The chips under a message that carried attachments: kind and size, which is all the server keeps.
function attachmentChips(markers: readonly AttachmentMarker[] | undefined, prefix: string) {
  if (!markers || markers.length === 0) return {}
  return {
    attachments: markers.map((marker, index) => ({
      id: `${prefix}-a${index}`,
      type: chipType(marker.kind),
      name: markerLabel(marker),
      status: { type: "complete" as const },
      content: [],
    })),
  }
}

// A reply's steps are tool-call parts ahead of its text. Each already finished (a step is only stored once the
// call has ended), so every part carries a result and the thread never shows a call still running; how it
// ended and its words travel in ``args`` for the step list to draw.
function stepParts(message: ChatMessage): ThreadMessageLikePart[] {
  if (message.role !== "assistant") return []
  return (message.steps ?? []).map((step, index) => ({
    type: "tool-call" as const,
    toolCallId: `m${message.id}-s${index}`,
    toolName: step.tool,
    args: { label: step.label, status: step.status, ...(step.summary ? { summary: step.summary } : {}) },
    result: step.status,
    ...(step.status === "failed" || step.status === "declined" ? { isError: true } : {}),
  }))
}

// What the thread renders for each stored message and notice is built once per object and then reused, so a poll
// that brings one new turn (or only a change of Jarvis's state) leaves every other turn's objects as they were and
// React redraws only what changed. The stored message objects themselves are kept across polls (``mergeMessages``).
const threadMessageCache = new WeakMap<ChatMessage, ThreadMessageLike>()
const noticeCache = new WeakMap<Notice, ThreadMessageLike[]>()

export function toThreadMessage(message: ChatMessage): ThreadMessageLike {
  const known = threadMessageCache.get(message)
  if (known) return known
  const built: ThreadMessageLike = {
    id: `m${message.id}`,
    role: message.role,
    content: [...stepParts(message), { type: "text", text: message.text }],
    createdAt: new Date(message.ts * 1000),
    ...(message.role === "user" ? attachmentChips(message.attachments, `m${message.id}`) : {}),
    ...(message.role === "assistant" ? { status: { type: "complete", reason: "stop" } as const } : {}),
    metadata: {
      ...(message.source === "voice" ? { modality: "voice" as const } : {}),
      custom: { source: message.source },
    },
  }
  threadMessageCache.set(message, built)
  return built
}

/** The thread's own conversion step: the page hands it messages that are already in the thread's shape. One
 * function for the page's lifetime, so the runtime keeps its cache of converted messages between renders. */
export const convertThreadMessage = (message: ThreadMessageLike): ThreadMessageLike => message

// A local line (busy, stopped, could not answer): shown, never stored. A failed request is shown
// first as the message the owner sent.
export function noticeMessages(notice: Notice): ThreadMessageLike[] {
  const known = noticeCache.get(notice)
  if (known) return known
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
  noticeCache.set(notice, out)
  return out
}

// The steps of the request in flight, drawn in its thinking placeholder as finished calls.
function liveParts(steps: readonly LiveStep[]): ThreadMessageLikePart[] {
  return steps.map((step, index) => ({
    type: "tool-call" as const,
    toolCallId: `live-s${index}`,
    toolName: step.tool,
    args: { label: step.label, status: step.status },
    result: step.status,
    ...(step.status === "failed" || step.status === "declined" ? { isError: true } : {}),
  }))
}

export function buildThreadMessages(
  messages: readonly ChatMessage[],
  notices: readonly Notice[],
  pending: Pending | null,
  liveSteps: readonly LiveStep[] = [],
  now: () => Date = () => new Date(),
): ThreadMessageLike[] {
  // Notices sit where they happened: after every message that is not newer than them.
  const out: ThreadMessageLike[] = []
  const placed = [...notices].sort((a, b) => a.ts - b.ts || a.id - b.id)
  let next = 0
  for (const message of messages) {
    while (next < placed.length && placed[next]!.ts < message.ts) out.push(...noticeMessages(placed[next++]!))
    out.push(toThreadMessage(message))
  }
  while (next < placed.length) out.push(...noticeMessages(placed[next++]!))
  if (pending) {
    const createdAt = now()
    out.push({
      id: "pending-user",
      role: "user",
      content: [{ type: "text", text: pending.text }],
      createdAt,
      ...attachmentChips(pending.attachments, "pending"),
    })
    if (!pending.stopped) {
      out.push({
        id: "pending-reply",
        role: "assistant",
        content: liveParts(liveSteps),
        createdAt,
        status: { type: "running" },
      })
    }
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

// The placeholder goes once Jarvis is no longer working on the request and either a reply newer than the
// message is already stored (a fast answer can land before the send is confirmed) or the send was confirmed
// and the snapshot comes from a poll made after that: an earlier poll's "not busy" can be from before the
// server had the request. ``snapshotPoll`` is that poll's number.
export function pendingDone(
  pending: Pending | null,
  busyQuery: boolean,
  messages: readonly ChatMessage[],
  snapshotPoll: number,
): boolean {
  if (!pending || busyQuery) return false
  if (messages.some((m) => m.role === "assistant" && m.id > pending.afterId)) return true
  return pending.accepted && snapshotPoll > pending.acceptedAt
}
