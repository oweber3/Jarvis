import { useAuiState } from "@assistant-ui/react"
import { BanIcon, CheckIcon, ChevronRightIcon, ClockIcon, XIcon } from "lucide-react"
import { useId, useState, type FC, type ReactNode } from "react"
import { cn } from "@/lib/utils"
import type { ToolStep } from "@/api"
import { stepEnding, stepFromArgs, stepsSummary, stepTone } from "@/steps"

// The tool calls Jarvis made for a reply, as compact lines ahead of its text: what was done in the owner's terms
// ("Opened Word", "Searched the web · 3 results"), and how it ended when it did not simply work ("Waiting for your
// confirmation"), so a step that needs the owner stands out. Written for Jarvis. Each line renders one of
// assistant-ui's tool-call parts (``conversion.ts``); consecutive ones form a group, and a long group folds into one
// line that opens to them all.

const FOLD_OVER = 3

const BADGE = {
  ok: "bg-[var(--hud-success-glow)] text-[var(--hud-success)]",
  problem: "bg-[var(--hud-error-glow)] text-destructive",
  pending: "bg-[var(--hud-warning-glow)] text-warning",
} as const
const ENDING_COLOUR = { ok: "text-muted-foreground", problem: "text-destructive", pending: "text-warning" } as const

export const ToolStepRow: FC<{ args?: unknown }> = ({ args }) => {
  const step = stepFromArgs(args)
  const tone = stepTone(step)
  const ending = stepEnding(step)
  const Icon = tone === "ok" ? CheckIcon : tone === "pending" ? ClockIcon : step.status === "declined" ? BanIcon : XIcon

  return (
    <div data-slot="aui_tool-step" data-status={step.status} className="flex min-w-0 items-start gap-2 py-0.5 text-[13px] leading-5">
      <span aria-hidden className={cn("mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full", BADGE[tone])}>
        <Icon className="size-2.5 stroke-[3]" />
      </span>
      <span className="min-w-0 wrap-break-word">
        <span className="text-foreground/90">{step.label}</span>
        {step.summary && <span className="text-muted-foreground"> · {step.summary}</span>}
        {ending ? (
          <span className={cn("font-medium", ENDING_COLOUR[tone])}> · {ending}</span>
        ) : (
          <span className="sr-only">, done</span>
        )}
      </span>
    </div>
  )
}

/** The steps of one reply: listed as they are, or folded into a line that opens to them when there are many. */
export const ToolStepGroup: FC<{ indices: readonly number[]; children: ReactNode }> = ({ indices, children }) => {
  const statuses = useAuiState((s) =>
    indices.map((index) => {
      const part = s.message.parts[index]
      return part && part.type === "tool-call" ? stepFromArgs(part.args).status : "done"
    }).join(" "),
  )
  const steps = statuses.split(" ").map((status) => ({ status: status as ToolStep["status"] }))
  const [open, setOpen] = useState(false)
  const listId = useId()
  const folds = indices.length > FOLD_OVER
  const needsYou = steps.some((s) => stepTone(s) !== "ok")

  return (
    <div data-slot="aui_tool-steps" role="group" aria-label="What Jarvis did" className="my-1.5 flex flex-col">
      {folds && (
        <button
          type="button"
          aria-expanded={open}
          aria-controls={listId}
          onClick={() => setOpen((value) => !value)}
          className={cn(
            "hover:text-foreground focus-visible:ring-ring/50 -ms-1 flex w-fit items-center gap-1 rounded-md px-1 py-0.5 text-[13px] outline-none focus-visible:ring-2",
            needsYou ? "text-foreground" : "text-muted-foreground",
          )}
        >
          <ChevronRightIcon
            aria-hidden
            className={cn("size-3.5 transition-transform motion-reduce:transition-none", open && "rotate-90")}
          />
          {stepsSummary(steps)}
        </button>
      )}
      <div id={listId} hidden={folds && !open} className={cn("flex flex-col", folds && "ms-1 border-s ps-3")}>
        {children}
      </div>
    </div>
  )
}
