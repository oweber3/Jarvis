import { afterEach, describe, expect, it, vi } from "vitest"
import { ApiError } from "@/api"
import { phoneApi } from "@/phone/phoneApi"

afterEach(() => vi.unstubAllGlobals())

describe("the reply audio", () => {
  it("is fetched only from the PC's own audio path", async () => {
    const fetcher = vi.fn()
    vi.stubGlobal("fetch", fetcher)
    for (const url of ["//elsewhere/api/audio/" + "a".repeat(32), "elsewhere/x", "/api/library",
      "/api/audio/short", "/api/audio/" + "A".repeat(32), "/api/audio/" + "a".repeat(32) + "/../x"]) {
      await expect(phoneApi.replyAudio(url)).rejects.toBeInstanceOf(ApiError)
    }
    expect(fetcher).not.toHaveBeenCalled()
  })

  it("is fetched from a real audio address", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(new Uint8Array([1, 2, 3])))
    vi.stubGlobal("fetch", fetcher)
    const clip = await phoneApi.replyAudio("/api/audio/" + "a".repeat(32))
    expect(new Uint8Array(clip)).toEqual(new Uint8Array([1, 2, 3]))
    expect(fetcher).toHaveBeenCalledOnce()
  })
})
