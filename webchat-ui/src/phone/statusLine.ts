import { MODE_LABELS } from "@/models"

// What the header's connection dot shows, and the details behind it: whether the PC can be reached, who
// answers, and how busy the PC is.

export type ConnectionInput = { reachable: boolean; ready: boolean; busy: boolean }

export type Connection = { tone: "good" | "warn" | "bad"; label: string }

/** The header's dot: green when the PC is connected and free, amber while busy or starting, red when unreachable. */
export function connection(input: ConnectionInput): Connection {
  if (!input.reachable) return { tone: "bad", label: "Can't reach the PC" }
  if (!input.ready) return { tone: "warn", label: "Jarvis is starting" }
  if (input.busy) return { tone: "warn", label: "Jarvis is busy" }
  return { tone: "good", label: "Connected" }
}

export type StatusInput = ConnectionInput & {
  mode: string | undefined
  pc: { cpu: number; ram: number } | null | undefined
}

export type StatusRow = { term: string; text: string }

/** The rows of the status details. Only the connection is shown while the PC cannot be reached. */
export function statusDetails(input: StatusInput): StatusRow[] {
  const rows: StatusRow[] = [{ term: "Connection", text: connection(input).label }]
  if (!input.reachable) return rows
  if (input.mode) rows.push({ term: "Replies", text: MODE_LABELS[input.mode] ?? input.mode })
  if (input.pc) {
    rows.push({ term: "PC CPU", text: `${Math.round(input.pc.cpu)}%` })
    rows.push({ term: "PC memory", text: `${Math.round(input.pc.ram)}%` })
  }
  return rows
}
