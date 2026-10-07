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

export type ChatMessage = {
  id: number
  role: "user" | "assistant"
  text: string
  ts: number
  source: "typed" | "voice" | "confirmed"
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
  const response = await fetch(path, init)
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
  send: (text: string) => request<{ query_id: number; status: string }>("POST", "/api/chat", { text }),
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
    default:
      return "That didn't work."
  }
}
