// The page's only door to Jarvis: the JSON API served beside it (jarvis/webchat/server.py, or phone access's
// jarvis/remote/server.py for the phone app). Everything is relative to the page's own origin, so nothing here
// can reach another host.

export type Project = { id: string; name: string; position: number }

export type Chat = {
  id: string
  project_id: string | null
  title: string
  created_at: number
  updated_at: number
  last_mode: string
  last_model: string
  pinned: boolean
}

// One tool call made for a reply. The words are the tool's own and never hold a path, address or window title.
export type ToolStep = {
  tool: string
  status: "done" | "failed" | "declined" | "waiting"
  label: string
  summary: string | null
}

// What a message carried, as the server stores it: the kind and the size, never a name or any content.
export type AttachmentKind = "image" | "text" | "screenshot"
export type AttachmentMarker = { kind: AttachmentKind; size: number }

export type ChatMessage = {
  id: number
  role: "user" | "assistant"
  text: string
  ts: number
  source: "typed" | "voice" | "confirmed"
  steps: ToolStep[]
  attachments?: AttachmentMarker[]
}

// What the server answers for an upload: an id to name it by when the message is sent, never its content.
export type UploadInfo = { id: string; kind: AttachmentKind; size: number }
export type AttachmentLimits = { max_upload_bytes: number; max_attachments: number; max_images: number; max_file_chars: number }
// Whether images can go with a message to the route that answers, and whether Screenshot works. A reason
// names why not (no_vision, cloud_no_images, cloud_not_ready; screen_awareness_off, screenshot_unavailable).
export type AttachmentSupport = {
  images: { ok: boolean; reason: string | null }
  screenshot: { ok: boolean; reason: string | null }
  limits: AttachmentLimits
}

export type SearchHit = { chat_id: string; message_id: number; snippet: string; ts: number }

export type Notice = { id: number; kind: string; text: string; request: string | null; ts: number }

export type ReplyModes = { mode: string; enabled: string[] }
export type LocalModelState = { current: string | null; switchable: boolean }
export type LocalModel = { id: string; name: string; installed: boolean }

// The Claude or Codex mode in use: its model and effort, and whether its bridge has reported the models.
export type CloudState = { mode: string; model: string; effort: string; ready: boolean }
export type CloudEffort = { id: string; is_default: boolean; description: string }
export type CloudModel = { id: string; name: string; is_default: boolean; description: string; efforts: CloudEffort[] }

export type Library = { projects: Project[]; chats: Chat[]; active_chat_id: string | null }

// What phone access adds to every poll (remote/remote.spec.md, Long poll). Absent in the web chat.
export type Confirmation = { id: string; tool: string; action: string; target: string; consequence: string; expires_in: number }
export type VoiceAudio = { status: "pending" | "ready" | "none"; url?: string }
export type PhoneSnapshot = {
  confirmation: Confirmation | null
  pc?: { cpu: number; ram: number } | null
  quick_actions: string[]
  allow_confirm: boolean
  voice: { available: boolean; reason: string | null; audio: Record<string, VoiceAudio> }
}

export type Snapshot = {
  rev: number
  library_rev: number
  /** Bumped when stored messages of the open chat are taken back (a rewind), so every page reloads it. */
  history_rev?: number
  /** The steps taken so far for the typed request in flight: tool, status and label. */
  live_steps?: Pick<ToolStep, "tool" | "status" | "label">[]
  ready: boolean
  state: string
  busy: boolean
  busy_query: boolean
  active_chat_id: string | null
  chat: Chat | null
  messages: ChatMessage[]
  notices: Notice[]
  mode: ReplyModes | null
  model: LocalModelState
  cloud: CloudState | null
  phone?: PhoneSnapshot
}

export type ModelsResponse = {
  mode: ReplyModes | null
  current: string | null
  switchable: boolean
  models: LocalModel[]
  cloud: (CloudState & { models: CloudModel[] }) | null
}

export class ApiError extends Error {
  status: number
  code: string

  constructor(status: number, code: string) {
    super(code)
    this.status = status
    this.code = code
  }
}

// The phone app signs every request with its device token; the web chat has none (it is loopback only).
let credentials: { token: string | null; onUnauthorized: (() => void) | null } = { token: null, onUnauthorized: null }

/** Sign requests with `token` (null to stop). `onUnauthorized` runs when the PC refuses the token. */
export function setCredentials(token: string | null, onUnauthorized: (() => void) | null = null): void {
  credentials = { token, onUnauthorized }
}

function signed(headers: Record<string, string>): Record<string, string> {
  if (credentials.token) headers["Authorization"] = `Bearer ${credentials.token}`
  return headers
}

export async function request<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const headers = signed({})
  const init: RequestInit = { method, headers }
  if (signal) init.signal = signal
  if (body !== undefined || method !== "GET") {
    init.body = JSON.stringify(body ?? {})
    headers["Content-Type"] = "application/json"
  }
  return answer<T>(await fetch(path, init))
}

// Bytes travel raw: the server decides what a file is from its bytes, so no name or path is sent.
export async function postBytes<T>(path: string, bytes: Blob | Uint8Array, signal?: AbortSignal): Promise<T> {
  const init: RequestInit = {
    method: "POST",
    headers: signed({ "Content-Type": "application/octet-stream" }),
    body: bytes as BodyInit,
  }
  if (signal) init.signal = signal
  return answer<T>(await fetch(path, init))
}

/** A binary answer (the reply audio), signed like every other request. */
export async function getBytes(path: string, signal?: AbortSignal): Promise<ArrayBuffer> {
  const init: RequestInit = { method: "GET", headers: signed({}) }
  if (signal) init.signal = signal
  const response = await fetch(path, init)
  if (!response.ok) {
    if (response.status === 401 && credentials.token) credentials.onUnauthorized?.()
    throw new ApiError(response.status, `http_${response.status}`)
  }
  return response.arrayBuffer()
}

async function answer<T>(response: Response): Promise<T> {
  const text = await response.text()
  let data: unknown = {}
  try {
    data = text ? JSON.parse(text) : {}
  } catch {
    data = {}
  }
  if (!response.ok) {
    if (response.status === 401 && credentials.token) credentials.onUnauthorized?.()
    const code = (data as { error?: string }).error ?? `http_${response.status}`
    throw new ApiError(response.status, code)
  }
  return data as T
}

// What a chat's page holds (``GET /api/chats/<id>``): oldest first, with whether the chat goes on either side.
export type ChatPage = { chat: Chat; messages: ChatMessage[]; has_earlier?: boolean; has_later?: boolean }

function pageQuery(page: { limit: number; before?: number; around?: number } | undefined): string {
  if (!page) return ""
  const parts = [`limit=${page.limit}`]
  if (page.before !== undefined) parts.push(`before=${page.before}`)
  if (page.around !== undefined) parts.push(`around=${page.around}`)
  return `?${parts.join("&")}`
}

export const api = {
  library: () => request<Library>("GET", "/api/library"),
  models: () => request<ModelsResponse>("GET", "/api/models"),
  suggestions: () => request<unknown>("GET", "/api/suggestions"),
  /** A chat's messages: all of them, or a page (the newest ``limit``, those ``before`` a message, or ``around`` one). */
  chat: (id: string, page?: { limit: number; before?: number; around?: number }) =>
    request<ChatPage>("GET", `/api/chats/${id}${pageQuery(page)}`),
  poll: (rev: number, after: number, signal: AbortSignal) =>
    request<Snapshot>("GET", `/api/poll?rev=${rev}&after=${after}`, undefined, signal),
  createChat: (projectId: string | null) =>
    request<{ chat: Chat }>("POST", "/api/chats", projectId ? { project_id: projectId } : {}),
  openChat: (id: string) => request<{ chat: Chat }>("POST", `/api/chats/${id}/open`),
  renameChat: (id: string, title: string) => request<unknown>("PATCH", `/api/chats/${id}`, { title }),
  moveChat: (id: string, projectId: string | null) =>
    request<unknown>("PATCH", `/api/chats/${id}`, { project_id: projectId }),
  pinChat: (id: string, pinned: boolean) => request<unknown>("PATCH", `/api/chats/${id}`, { pinned }),
  search: (query: string, signal?: AbortSignal) =>
    request<{ results: SearchHit[] }>("GET", `/api/search?q=${encodeURIComponent(query)}`, undefined, signal),
  deleteChat: (id: string) => request<unknown>("DELETE", `/api/chats/${id}`),
  createProject: (name: string) => request<Project>("POST", "/api/projects", { name }),
  renameProject: (id: string, name: string) => request<unknown>("PATCH", `/api/projects/${id}`, { name }),
  deleteProject: (id: string) => request<unknown>("DELETE", `/api/projects/${id}`),
  send: (text: string, attachments: readonly string[] = [], speak = false) =>
    request<{ query_id: number; status: string }>("POST", "/api/chat", {
      text,
      ...(attachments.length ? { attachments } : {}),
      ...(speak ? { speak: true } : {}),
    }),
  /** Ask a stored message of the open chat again, as it was or edited; it and every later message are replaced.
   * ``newestId`` is the newest stored message the page shows: the server changes nothing if the chat's is another. */
  rewind: (chatId: string, messageId: number, newestId: number, text?: string, speak = false) =>
    request<{ query_id: number; status: string }>("POST", `/api/chats/${chatId}/rewind`, {
      message_id: messageId,
      newest_id: newestId,
      ...(text !== undefined ? { text } : {}),
      ...(speak ? { speak: true } : {}),
    }),
  attachmentSupport: () => request<AttachmentSupport>("GET", "/api/attachments/support"),
  uploadAttachment: (file: Blob) => postBytes<UploadInfo>("/api/attachments", file),
  captureScreenshot: () => request<UploadInfo>("POST", "/api/attachments/screenshot"),
  discardAttachment: (id: string) => request<unknown>("DELETE", `/api/attachments/${id}`),
  stop: () => request<unknown>("POST", "/api/stop"),
  setModel: (kind: "mode" | "local", value: string) => request<unknown>("POST", "/api/model", { kind, value }),
  setCloudModel: (model: string, effort?: string) =>
    request<unknown>("POST", "/api/model", { kind: "cloud", value: model, ...(effort !== undefined ? { effort } : {}) }),
  clear: () => request<unknown>("POST", "/api/clear", { confirm: true }),
}

// What the page says when Jarvis refuses something. Codes are the server's; the wording is the page's.
export function describeError(error: unknown): string {
  if (!(error instanceof ApiError)) return "Jarvis could not be reached."
  switch (error.code) {
    case "bad_query":
      return "That search isn't valid."
    case "busy":
      return "Jarvis is busy with another request. Try again in a moment."
    case "unavailable":
      return "Jarvis isn't ready yet."
    case "not_enabled":
      return "That reply mode isn't allowed in Settings."
    case "start_failed":
      return "That reply mode could not start."
    case "not_ready":
      return "Jarvis hasn't finished checking the available models. Try again in a moment."
    case "effort_unsupported":
      return "That model doesn't offer that effort level."
    case "not_cloud":
      return "Choose Claude or ChatGPT (Codex) first."
    case "not_installed":
      return "That model isn't installed. Install it with Ollama first."
    case "not_offered":
      return "Jarvis doesn't offer that model."
    case "unsupported_provider":
      return "Switching the local model needs the Ollama provider."
    case "save_failed":
      return "The choice could not be saved."
    case "empty":
      return "That file is empty."
    case "too_large":
      return "That file is too large to attach."
    case "unsupported":
      return "I can attach images (PNG, JPEG, GIF, WebP), PDFs with text, and text or code files. That is something else."
    case "unreadable":
      return "That file could not be read. It may be damaged."
    case "no_text":
      return "That PDF has no text I can read (it may be a scan)."
    case "encrypted":
      return "That PDF is password protected."
    case "too_many":
      return "That is more than one message can carry. Remove one first."
    case "unknown_attachment":
      return "An attachment expired before it was sent. Attach it again."
    case "bad_attachments":
      return "The attachments could not be read. Attach them again."
    case "no_vision":
      return "This local model can't see images. Switch to Claude, ChatGPT (Codex) or a model that can."
    case "cloud_no_images":
      return "This reply mode can't take images. Switch to Local or remove the image."
    case "cloud_not_ready":
      return "That reply mode is still starting. Try again in a moment."
    case "screen_awareness_off":
      return "Screen awareness is off. Turn it on in Settings to let me look at your screen."
    case "screenshot_unavailable":
      return "I couldn't capture the screen just now."
    case "not_open":
      return "That chat isn't the open one any more."
    case "changed":
      return "This chat changed since the page last showed it, so nothing was taken back. Have a look and try again."
    case "out_of_step":
      return "That message can't be asked again just now. Send it as a new message instead."
    case "not_user":
    case "unknown":
      return "That message is no longer in this chat."
    default:
      return "That didn't work."
  }
}
