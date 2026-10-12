import type { AttachmentKind, AttachmentLimits, AttachmentMarker, AttachmentSupport } from "@/api"

// What the page knows about attachments that needs no React: how they are named and sized, what blocks a
// send and why, and the marker file the Screenshot button hands to the composer. Covered by tests.

const KIND_NAMES: Record<AttachmentKind, string> = { image: "Image", text: "File", screenshot: "Screenshot" }

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  const megabytes = bytes / (1024 * 1024)
  return `${Number.isInteger(megabytes) ? megabytes : megabytes.toFixed(1)} MB`
}

// A sent message keeps only the kind and size of its attachments, so that is all it can show.
export function markerLabel(marker: AttachmentMarker): string {
  return `${KIND_NAMES[marker.kind]} · ${formatSize(marker.size)}`
}

// assistant-ui's own attachment types are "image", "document" and "file"; a screenshot is its own.
export function chipType(kind: AttachmentKind): string {
  return kind === "text" ? "document" : kind
}

export function kindOfType(type: string): AttachmentKind {
  return type === "image" ? "image" : type === "screenshot" ? "screenshot" : "text"
}

// -- the Screenshot button -------------------------------------------------------------------------

export const SCREENSHOT_NAME = "Screenshot"
const SCREENSHOT_TYPE = "application/x-jarvis-screenshot"

// The composer only takes files, so pressing Screenshot adds an empty marker file that the adapter turns
// into a capture request. It carries nothing and is never uploaded.
export function screenshotRequestFile(): File {
  return new File([], SCREENSHOT_NAME, { type: SCREENSHOT_TYPE })
}

export function isScreenshotRequest(file: Pick<File, "name" | "type" | "size">): boolean {
  return file.name === SCREENSHOT_NAME && file.type === SCREENSHOT_TYPE && file.size === 0
}

// -- before anything is sent -----------------------------------------------------------------------

export function checkFile(file: Pick<File, "size">, limits: AttachmentLimits | null): string | null {
  if (file.size === 0) return "That file is empty."
  if (limits && file.size > limits.max_upload_bytes) {
    return `That file is too large to attach (the limit is ${formatSize(limits.max_upload_bytes)}).`
  }
  return null
}

// ``problem`` marks a refusal the owner has to act on; a plain hint (a message is still needed) is not one.
export type SendGate = { blocked: boolean; message: string | null; problem?: boolean }

// Whether the message can go, and what to tell the owner. This mirrors the server's own refusals so a
// message is never lost to one: the server stays the authority, this only explains it early.
export function sendGate(
  support: AttachmentSupport | null,
  kinds: readonly AttachmentKind[],
  state: { uploading: boolean; hasText: boolean },
): SendGate {
  if (kinds.length === 0) return { blocked: false, message: null }
  if (state.uploading) return { blocked: true, message: null }
  if (!state.hasText) return { blocked: true, message: "Add a message to go with the attachment." }
  const images = kinds.includes("image")
  const sees = images || kinds.includes("screenshot")
  const reason = support && !support.images.ok ? support.images.reason : null
  if (reason === "cloud_not_ready" && sees) {
    return { blocked: true, message: "That reply mode is still starting. Try again in a moment.", problem: true }
  }
  if (reason === "cloud_no_images" && sees) {
    return { blocked: true, message: "This reply mode can't take images. Remove the image or switch to Local.", problem: true }
  }
  if (reason === "no_vision") {
    if (images) {
      return {
        blocked: true,
        message: "This local model can't see images. Remove the image, or switch to Claude, ChatGPT (Codex) or a model that can.",
        problem: true,
      }
    }
    if (sees) return { blocked: false, message: "Text only: this model can't see images, so it will read the text on the screen." }
  }
  return { blocked: false, message: null }
}

const CLOUD_NAMES: Record<string, string> = { claude: "Claude", codex: "ChatGPT (Codex)" }

// Attaching to a cloud mode sends it to that provider with the message.
export function cloudNote(mode: string | null | undefined): string | null {
  const name = mode ? CLOUD_NAMES[mode] : undefined
  return name ? `Will be sent to ${name}` : null
}
