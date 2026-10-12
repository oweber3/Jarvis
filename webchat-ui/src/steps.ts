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
