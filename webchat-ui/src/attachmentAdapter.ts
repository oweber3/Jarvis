import type { AttachmentAdapter, CompleteAttachment, PendingAttachment } from "@assistant-ui/react"
import { describeError, type AttachmentKind, type AttachmentLimits, type UploadInfo } from "@/api"
import { checkFile, chipType, isScreenshotRequest, SCREENSHOT_NAME } from "@/attachments"

// Connects assistant-ui's composer to the server's upload endpoint. The server holds each upload (in memory)
// under an id until the message that names it is sent; this keeps the page's own chip ids apart from those
// ids, so a chip can show an upload that is still travelling.

export type AttachmentApi = {
  upload: (file: Blob) => Promise<UploadInfo>
  screenshot: () => Promise<UploadInfo>
  discard: (id: string) => Promise<unknown>
}

export type HeldUpload = { serverId: string; kind: AttachmentKind; size: number }

let counter = 0
const newId = () => `att-${Date.now().toString(36)}-${(counter++).toString(36)}`

export class JarvisAttachmentAdapter implements AttachmentAdapter {
  // The server decides what a file is from its content, so any file may be offered.
  accept = "*"
  private readonly held = new Map<string, HeldUpload>()

  constructor(
    private readonly api: AttachmentApi,
    private readonly limits: () => AttachmentLimits | null,
    private readonly report: (message: string) => void,
  ) {}

  async *add({ file }: { file: File }): AsyncGenerator<PendingAttachment, void> {
    const id = newId()
    const screenshot = isScreenshotRequest(file)
    const base = {
      id,
      type: screenshot ? "screenshot" : file.type.startsWith("image/") ? "image" : "document",
      name: screenshot ? SCREENSHOT_NAME : file.name,
      contentType: file.type,
      file,
    }
    const max = this.limits()?.max_attachments
    if (max !== undefined && this.held.size >= max) this.refuse(`You can attach up to ${max} things to one message.`)
    const problem = screenshot ? null : checkFile(file, this.limits())
    if (problem) this.refuse(problem)
    yield { ...base, status: { type: "running", reason: "uploading", progress: 0 } }
    let info: UploadInfo
    try {
      info = screenshot ? await this.api.screenshot() : await this.api.upload(file)
    } catch (error) {
      // The chip is already showing, so it carries the reason; there is no second message.
      throw new Error(describeError(error))
    }
    this.held.set(id, { serverId: info.id, kind: info.kind, size: info.size })
    yield { ...base, type: chipType(info.kind), status: { type: "requires-action", reason: "composer-send" } }
  }

  private refuse(message: string): never {
    this.report(message)
    throw new Error(message)
  }

  async remove(attachment: { id: string; status: { type: string } }): Promise<void> {
    const held = this.held.get(attachment.id)
    this.held.delete(attachment.id)
    if (held) await this.api.discard(held.serverId).catch(() => undefined)
  }

  // Nothing travels with the file here: the server already has it, and the message names it by id.
  async send(attachment: PendingAttachment): Promise<CompleteAttachment> {
    return { ...attachment, status: { type: "complete" }, content: [] }
  }

  /** What the server holds for these chips, handed over once as the message goes. */
  take(ids: readonly string[]): HeldUpload[] {
    const taken: HeldUpload[] = []
    for (const id of ids) {
      const held = this.held.get(id)
      if (held) {
        taken.push(held)
        this.held.delete(id)
      }
    }
    return taken
  }
}
