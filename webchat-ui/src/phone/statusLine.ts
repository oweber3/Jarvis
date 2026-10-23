import { MODE_LABELS } from "@/models"

// The one line under the orb: whether the PC can be reached, who answers, and how busy the PC is.

export type StatusInput = {
  reachable: boolean
  ready: boolean
  mode: string | undefined
  pc: { cpu: number; ram: number } | null | undefined
}

export type StatusPart = { text: string; tone: "good" | "warn" | "muted" }

export function statusParts(input: StatusInput): StatusPart[] {
  const parts: StatusPart[] = []
  if (!input.reachable) parts.push({ text: "Can't reach the PC", tone: "warn" })
  else if (!input.ready) parts.push({ text: "Jarvis is starting", tone: "warn" })
  else parts.push({ text: "Connected", tone: "good" })
  if (input.reachable && input.mode) parts.push({ text: MODE_LABELS[input.mode] ?? input.mode, tone: "muted" })
  if (input.reachable && input.pc) {
    parts.push({ text: `CPU ${Math.round(input.pc.cpu)}%`, tone: "muted" })
    parts.push({ text: `RAM ${Math.round(input.pc.ram)}%`, tone: "muted" })
  }
  return parts
}
