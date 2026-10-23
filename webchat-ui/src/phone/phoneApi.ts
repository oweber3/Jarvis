import { ApiError, getBytes, postBytes, request } from "@/api"
import type { SpokenReply } from "@/phone/voiceSession"

// The phone-only part of the API (remote/remote.spec.md, HTTP API); the chat routes are the shared client's.

const AUDIO_PATH = /^\/api\/audio\/[0-9a-f]{32}$/

export const phoneApi = {
  pair: (code: string, name: string) =>
    request<{ token: string; device_id: string }>("POST", "/api/pair", { code, name }),
  confirm: (id: string, approve: boolean) => request<unknown>("POST", "/api/confirm", { id, approve }),
  unpair: () => request<unknown>("POST", "/api/unpair"),
  /** A spoken request: raw 16 kHz mono PCM16. With attachments, the only request whose body is not JSON. */
  async speak(pcm: Uint8Array, signal: AbortSignal): Promise<SpokenReply> {
    const reply = await postBytes<{ status: string; text?: string; query_id?: number }>("/api/voice", pcm, signal)
    if (reply.status === "nothing_heard") return { status: "nothing_heard" }
    if (reply.status === "accepted" && typeof reply.query_id === "number") {
      return { status: "accepted", text: reply.text ?? "", query_id: reply.query_id }
    }
    throw new ApiError(502, "bad_reply")
  },
  /** The reply audio, from its one-shot address. Only the PC's own audio path is ever fetched with the token. */
  replyAudio(url: string): Promise<ArrayBuffer> {
    if (!AUDIO_PATH.test(url)) return Promise.reject(new ApiError(502, "bad_reply"))
    return getBytes(url)
  },
}

// What the pairing screen says when the code is refused.
export function describePairError(error: unknown): string {
  if (!(error instanceof ApiError)) return "Jarvis could not be reached. Is the PC on and this phone on the same network?"
  if (error.status === 403) return "That code didn't work. Check it, or start a new one on the PC."
  if (error.status === 415 || error.status === 400) return "That didn't look right. Try again."
  return "Pairing didn't work. Try again."
}
