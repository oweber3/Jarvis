// The page's only door to Jarvis: the JSON API served beside it (jarvis/webchat/server.py).
// Everything is relative to the page's own origin, so nothing here can reach another host.

export type Project = { id: string; name: string; position: number }

export type Chat = {
  id: string
  project_id: string | null
  title: string
  created_at: number
  updated_at: number
  last_mode: string
  last_model: string
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

export type Notice = { id: number; kind: string; text: string; request: string | null; ts: number }

export type ReplyModes = { mode: string; enabled: string[] }
export type LocalModelState = { current: string | null; switchable: boolean }
export type LocalModel = { id: string; name: string; installed: boolean }

// The Claude or Codex mode in use: its model and effort, and whether its bridge has reported the models.
export type CloudState = { mode: string; model: string; effort: string; ready: boolean }
export type CloudEffort = { id: string; is_default: boolean; description: string }
export type CloudModel = { id: string; name: string; is_default: boolean; description: string; efforts: CloudEffort[] }

export type Library = { projects: Project[]; chats: Chat[]; active_chat_id: string | null }

export type Snapshot = {
  rev: number
  library_rev: number
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

async function request<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const headers: Record<string, string> = {}
  const init: RequestInit = { method, headers }
  if (signal) init.signal = signal
  if (body !== undefined || method !== "GET") {
    init.body = JSON.stringify(body ?? {})
    headers["Content-Type"] = "application/json"
  }
  return answer<T>(await fetch(path, init))
}

// A file travels as its raw bytes: the server decides what it is from the bytes, so no name or path is sent.
async function upload<T>(path: string, file: Blob): Promise<T> {
  return answer<T>(await fetch(path, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file }))
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
    const code = (data as { error?: string }).error ?? `http_${response.status}`
    throw new ApiError(response.status, code)
  }
  return data as T
}

export const api = {
  library: () => request<Library>("GET", "/api/library"),
  models: () => request<ModelsResponse>("GET", "/api/models"),
  chat: (id: string) => request<{ chat: Chat; messages: ChatMessage[] }>("GET", `/api/chats/${id}`),
  poll: (rev: number, after: number, signal: AbortSignal) =>
    request<Snapshot>("GET", `/api/poll?rev=${rev}&after=${after}`, undefined, signal),
  createChat: (projectId: string | null) =>
    request<{ chat: Chat }>("POST", "/api/chats", projectId ? { project_id: projectId } : {}),
  openChat: (id: string) => request<{ chat: Chat }>("POST", `/api/chats/${id}/open`),
  renameChat: (id: string, title: string) => request<unknown>("PATCH", `/api/chats/${id}`, { title }),
  moveChat: (id: string, projectId: string | null) =>
    request<unknown>("PATCH", `/api/chats/${id}`, { project_id: projectId }),
  deleteChat: (id: string) => request<unknown>("DELETE", `/api/chats/${id}`),
  createProject: (name: string) => request<Project>("POST", "/api/projects", { name }),
  renameProject: (id: string, name: string) => request<unknown>("PATCH", `/api/projects/${id}`, { name }),
  deleteProject: (id: string) => request<unknown>("DELETE", `/api/projects/${id}`),
  send: (text: string, attachments: readonly string[] = []) =>
    request<{ query_id: number; status: string }>("POST", "/api/chat", { text, ...(attachments.length ? { attachments } : {}) }),
  attachmentSupport: () => request<AttachmentSupport>("GET", "/api/attachments/support"),
  uploadAttachment: (file: Blob) => upload<UploadInfo>("/api/attachments", file),
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
    default:
      return "That didn't work."
  }
}
