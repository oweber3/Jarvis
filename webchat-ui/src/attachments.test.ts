import { describe, expect, it, vi } from "vitest"
import { ApiError, describeError, type AttachmentSupport, type ChatMessage } from "@/api"
import {
  checkFile,
  cloudNote,
  formatSize,
  isScreenshotRequest,
  kindOfType,
  markerLabel,
  screenshotRequestFile,
  sendGate,
} from "@/attachments"
import { JarvisAttachmentAdapter, type AttachmentApi } from "@/attachmentAdapter"
import { buildThreadMessages } from "@/conversion"

const limits = { max_upload_bytes: 10 * 1024 * 1024, max_attachments: 4, max_images: 3, max_file_chars: 20000 }
const support = (images: AttachmentSupport["images"], screenshot: AttachmentSupport["screenshot"] = { ok: true, reason: null }): AttachmentSupport => ({
  images,
  screenshot,
  limits,
})
const sees = support({ ok: true, reason: null })
const blind = support({ ok: false, reason: "no_vision" })
const noCloudImages = support({ ok: false, reason: "cloud_no_images" })
const notReady = support({ ok: false, reason: "cloud_not_ready" })
const idle = { uploading: false, hasText: true }

describe("formatSize", () => {
  it("reads plainly at every scale", () => {
    expect(formatSize(0)).toBe("0 B")
    expect(formatSize(512)).toBe("512 B")
    expect(formatSize(2048)).toBe("2 KB")
    expect(formatSize(86_000)).toBe("84 KB")
    expect(formatSize(1_572_864)).toBe("1.5 MB")
    expect(formatSize(10 * 1024 * 1024)).toBe("10 MB")
  })
})

describe("markerLabel", () => {
  it("names the kind and the size, never a file name", () => {
    expect(markerLabel({ kind: "image", size: 86_000 })).toBe("Image · 84 KB")
    expect(markerLabel({ kind: "text", size: 512 })).toBe("File · 512 B")
    expect(markerLabel({ kind: "screenshot", size: 2048 })).toBe("Screenshot · 2 KB")
  })
})

describe("the Screenshot button", () => {
  it("asks for a capture with a marker file that is not a real file", () => {
    const marker = screenshotRequestFile()
    expect(isScreenshotRequest(marker)).toBe(true)
    expect(isScreenshotRequest(new File(["x"], "Screenshot", { type: "application/x-jarvis-screenshot" }))).toBe(false)
    expect(isScreenshotRequest(new File([], "Screenshot.png", { type: "image/png" }))).toBe(false)
  })
})

describe("checkFile", () => {
  it("refuses an empty file and one over the limit before anything is sent", () => {
    expect(checkFile(new File([], "a.txt"), limits)).toMatch(/empty/i)
    const huge = { name: "big.bin", size: limits.max_upload_bytes + 1 } as File
    expect(checkFile(huge, limits)).toMatch(/too large/i)
    expect(checkFile(new File(["hello"], "a.txt"), limits)).toBeNull()
  })
})

describe("kindOfType", () => {
  it("maps the chip types back to the server's kinds", () => {
    expect(kindOfType("image")).toBe("image")
    expect(kindOfType("screenshot")).toBe("screenshot")
    expect(kindOfType("document")).toBe("text")
    expect(kindOfType("file")).toBe("text")
  })
})

describe("sendGate", () => {
  it("lets everything through when images are possible", () => {
    expect(sendGate(sees, ["image", "text", "screenshot"], idle)).toEqual({ blocked: false, message: null })
  })

  it("lets text files through a model that cannot see", () => {
    expect(sendGate(blind, ["text"], idle).blocked).toBe(false)
  })

  it("blocks an image for a local model without vision and says why", () => {
    const gate = sendGate(blind, ["image"], idle)
    expect(gate.blocked).toBe(true)
    expect(gate.message).toMatch(/can't see images/i)
  })

  it("lets a screenshot through a blind model but tells the owner it is text only", () => {
    const gate = sendGate(blind, ["screenshot"], idle)
    expect(gate.blocked).toBe(false)
    expect(gate.message).toMatch(/text only/i)
  })

  it("blocks images and screenshots for a cloud mode that takes none", () => {
    expect(sendGate(noCloudImages, ["screenshot"], idle).blocked).toBe(true)
    expect(sendGate(noCloudImages, ["image"], idle).blocked).toBe(true)
    expect(sendGate(noCloudImages, ["text"], idle).blocked).toBe(false)
  })

  it("waits for a cloud mode that is still starting", () => {
    const gate = sendGate(notReady, ["image"], idle)
    expect(gate.blocked).toBe(true)
    expect(gate.message).toMatch(/starting/i)
  })

  it("waits for uploads in progress without scolding", () => {
    expect(sendGate(sees, ["image"], { uploading: true, hasText: true })).toEqual({ blocked: true, message: null })
  })

  it("asks for words to go with an attachment", () => {
    const gate = sendGate(sees, ["image"], { uploading: false, hasText: false })
    expect(gate.blocked).toBe(true)
    expect(gate.message).toMatch(/message/i)
  })

  it("blocks nothing before the page knows what is possible", () => {
    expect(sendGate(null, ["image"], idle).blocked).toBe(false)
  })

  it("has nothing to say when nothing is attached", () => {
    expect(sendGate(blind, [], { uploading: false, hasText: false })).toEqual({ blocked: false, message: null })
  })
})

describe("cloudNote", () => {
  it("says an attachment goes to the provider only in a cloud mode", () => {
    expect(cloudNote("claude")).toBe("Will be sent to Claude")
    expect(cloudNote("codex")).toBe("Will be sent to ChatGPT (Codex)")
    expect(cloudNote(null)).toBeNull()
    expect(cloudNote("local")).toBeNull()
  })
})

describe("what the owner is told when an attachment is refused", () => {
  const told = (code: string, status = 400) => describeError(new ApiError(status, code))

  it("explains every reason the server gives", () => {
    for (const code of [
      "empty",
      "too_large",
      "unsupported",
      "unreadable",
      "no_text",
      "encrypted",
      "too_many",
      "unknown_attachment",
      "no_vision",
      "cloud_no_images",
      "cloud_not_ready",
      "screen_awareness_off",
      "screenshot_unavailable",
      "bad_attachments",
    ]) {
      expect(told(code), code).not.toBe("That didn't work.")
    }
  })

  it("names what can be done about the common ones", () => {
    expect(told("unsupported")).toMatch(/image|PDF|text/i)
    expect(told("no_text")).toMatch(/text/i)
    expect(told("screen_awareness_off")).toMatch(/Settings/)
    expect(told("no_vision")).toMatch(/Claude|vision/i)
  })
})

describe("the sent message", () => {
  const stored = (attachments: ChatMessage["attachments"]): ChatMessage => ({
    id: 1,
    role: "user",
    text: "what is this?",
    ts: 1_760_000_000,
    source: "typed",
    attachments,
  })

  it("shows chips with the kind and size of what it carried", () => {
    const [message] = buildThreadMessages([stored([{ kind: "image", size: 86_000 }, { kind: "text", size: 512 }])], [], null)
    const attachments = (message as { attachments?: { type: string; name: string }[] }).attachments
    expect(attachments?.map((a) => [a.type, a.name])).toEqual([
      ["image", "Image · 84 KB"],
      ["document", "File · 512 B"],
    ])
  })

  it("has no attachment list when it carried nothing", () => {
    const [message] = buildThreadMessages([stored([])], [], null)
    expect("attachments" in message!).toBe(false)
  })

  it("shows the chips at once on the message being sent", () => {
    const pending = { text: "look", accepted: false, afterId: 0, acceptedAt: 0, attachments: [{ kind: "screenshot" as const, size: 2048 }] }
    const out = buildThreadMessages([], [], pending)
    const attachments = (out[0] as { attachments?: { name: string }[] }).attachments
    expect(attachments?.map((a) => a.name)).toEqual(["Screenshot · 2 KB"])
  })
})

describe("the attachment adapter", () => {
  const fakeApi = (overrides: Partial<AttachmentApi> = {}): AttachmentApi & { discarded: string[] } => {
    const discarded: string[] = []
    return {
      upload: vi.fn(async (file: Blob) => ({ id: "ab12", kind: "image" as const, size: file.size })),
      screenshot: vi.fn(async () => ({ id: "cd34", kind: "screenshot" as const, size: 4096 })),
      discard: vi.fn(async (id: string) => {
        discarded.push(id)
      }),
      discarded,
      ...overrides,
    }
  }
  const collect = async (stream: AsyncGenerator<{ status: { type: string } } & Record<string, unknown>, void>) => {
    const seen: ({ status: { type: string } } & Record<string, unknown>)[] = []
    for await (const item of stream) seen.push(item)
    return seen
  }
  const build = (api = fakeApi(), report = vi.fn()) => ({
    adapter: new JarvisAttachmentAdapter(api, () => limits, report),
    api,
    report,
  })

  it("shows the file uploading, then ready to send", async () => {
    const { adapter, api } = build()
    const file = new File(["pixels"], "photo.png", { type: "image/png" })
    const steps = await collect(adapter.add({ file }) as AsyncGenerator<never, void>)
    expect(steps.map((s) => s.status.type)).toEqual(["running", "requires-action"])
    expect(steps[0]!.name).toBe("photo.png")
    expect(steps[0]!.id).toBe(steps[1]!.id)
    expect(api.upload).toHaveBeenCalledWith(file)
  })

  it("sends the screenshot marker to the capture endpoint, not as a file", async () => {
    const { adapter, api } = build()
    const steps = await collect(adapter.add({ file: screenshotRequestFile() }) as AsyncGenerator<never, void>)
    expect(api.screenshot).toHaveBeenCalledOnce()
    expect(api.upload).not.toHaveBeenCalled()
    expect(steps.at(-1)!.type).toBe("screenshot")
    expect(steps.at(-1)!.name).toBe("Screenshot")
  })

  it("names the file as the owner knows it and types the chip by what the server found", async () => {
    const { adapter } = build(fakeApi({ upload: vi.fn(async () => ({ id: "ab12", kind: "text" as const, size: 5 })) }))
    const steps = await collect(adapter.add({ file: new File(["hello"], "notes.png", { type: "image/png" }) }) as AsyncGenerator<never, void>)
    expect(steps.at(-1)!.type).toBe("document")
  })

  it("fails the chip with the reason in words, and says it nowhere else", async () => {
    const { adapter, report } = build(fakeApi({ upload: vi.fn(async () => { throw new ApiError(415, "unsupported") }) }))
    const file = new File(["MZ"], "tool.exe")
    await expect(collect(adapter.add({ file }) as AsyncGenerator<never, void>)).rejects.toThrow(/image|PDF|text/i)
    expect(report).not.toHaveBeenCalled()
  })

  it("does not even try a file that is over the limit", async () => {
    const { adapter, api, report } = build()
    const huge = { name: "big.bin", size: limits.max_upload_bytes + 1, type: "" } as File
    await expect(collect(adapter.add({ file: huge }) as AsyncGenerator<never, void>)).rejects.toThrow(/too large/i)
    expect(api.upload).not.toHaveBeenCalled()
    expect(report).toHaveBeenCalled()
  })

  it("refuses a fifth attachment on one message", async () => {
    const { adapter, report } = build()
    for (let i = 0; i < limits.max_attachments; i++) {
      await collect(adapter.add({ file: new File(["x"], `f${i}.txt`) }) as AsyncGenerator<never, void>)
    }
    await expect(collect(adapter.add({ file: new File(["x"], "one-more.txt") }) as AsyncGenerator<never, void>)).rejects.toThrow()
    expect(report).toHaveBeenCalled()
  })

  it("hands over what the server holds, once, when the message goes", async () => {
    const { adapter } = build()
    const steps = await collect(adapter.add({ file: new File(["x"], "a.png", { type: "image/png" }) }) as AsyncGenerator<never, void>)
    const id = steps.at(-1)!.id as string
    expect(adapter.take([id])).toEqual([{ serverId: "ab12", kind: "image", size: 1 }])
    expect(adapter.take([id])).toEqual([])
  })

  it("withdraws a removed attachment from the server", async () => {
    const { adapter, api } = build()
    const steps = await collect(adapter.add({ file: new File(["x"], "a.png", { type: "image/png" }) }) as AsyncGenerator<never, void>)
    const last = steps.at(-1)!
    await adapter.remove(last as never)
    expect(api.discarded).toEqual(["ab12"])
    expect(adapter.take([last.id as string])).toEqual([])
  })

  it("has nothing to withdraw for a chip that failed", async () => {
    const { adapter, api } = build()
    await adapter.remove({ id: "never-uploaded", status: { type: "incomplete", reason: "error" } } as never)
    expect(api.discard).not.toHaveBeenCalled()
  })

  it("completes an attachment without changing what the chip shows", async () => {
    const { adapter } = build()
    const steps = await collect(adapter.add({ file: new File(["x"], "a.png", { type: "image/png" }) }) as AsyncGenerator<never, void>)
    const sent = await adapter.send(steps.at(-1) as never)
    expect(sent.status).toEqual({ type: "complete" })
    expect(sent.id).toBe(steps.at(-1)!.id)
  })
})
