import type { ToolStep } from "@/api"

// Words for the tool steps shown under a reply. The labels and summaries come from Jarvis (each tool words
// its own); only how a step ended is worded here. Pure functions, covered by tests.

const ENDED: Record<string, string> = {
  done: "Done",
  failed: "Didn't work",
  declined: "Not allowed",
  waiting: "Waiting for your confirmation",
}

export type StepTone = "ok" | "problem" | "pending"

export function stepTone(step: Pick<ToolStep, "status">): StepTone {
  if (step.status === "failed" || step.status === "declined") return "problem"
  return step.status === "waiting" ? "pending" : "ok"
}

// How a step ended, said beside its label; nothing for one that worked, whose tick says it.
export function stepEnding(step: Pick<ToolStep, "status">): string | null {
  return step.status === "done" ? null : (ENDED[step.status] ?? step.status)
}

// The line a reply's many steps fold into: how many, and anything that did not go as asked.
export function stepsSummary(steps: readonly Pick<ToolStep, "status">[]): string {
  const count = `${steps.length} ${steps.length === 1 ? "step" : "steps"}`
  const problems = steps.filter((s) => stepTone(s) === "problem").length
  if (problems > 0) return `${count} · ${problems} didn't work or weren't allowed`
  if (steps.some((s) => s.status === "waiting")) return `${count} · waiting for your confirmation`
  return count
}

// A step of the request in flight, as the poll carries it: what was found arrives with the reply.
export type LiveStep = Pick<ToolStep, "tool" | "status" | "label">

// What opens when a step is expanded: how it ended, then what it found when the tool said.
export function expandedLines(step: Pick<ToolStep, "status" | "summary">): string[] {
  const lines = [ENDED[step.status] ?? step.status]
  if (step.summary) lines.push(step.summary)
  return lines
}

// The step a tool-call part carries (``conversion.ts``), read back defensively: the part's ``args`` is JSON.
export function stepFromArgs(args: unknown): Pick<ToolStep, "label" | "status" | "summary"> {
  const bag = (args && typeof args === "object" ? args : {}) as Record<string, unknown>
  const text = (value: unknown): string | null => (typeof value === "string" && value.trim() ? value : null)
  return {
    label: text(bag["label"]) ?? "Step",
    status: (text(bag["status"]) ?? "done") as ToolStep["status"],
    summary: text(bag["summary"]),
  }
}
