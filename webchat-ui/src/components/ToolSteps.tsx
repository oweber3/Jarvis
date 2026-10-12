import { cn } from "@/lib/utils"
import { expandedLines, stepFromArgs, stepTone } from "@/steps"
import { CheckIcon, ChevronRightIcon, ClockIcon, XIcon } from "lucide-react"
import { useId, useState, type FC } from "react"

const TONE_ICON = { ok: CheckIcon, problem: XIcon, pending: ClockIcon } as const
const TONE_COLOUR = { ok: "text-muted-foreground", problem: "text-destructive", pending: "text-muted-foreground" } as const

/**
 * One tool call Jarvis made for a reply, as a compact row ("Opened Word"). It opens to how the call ended and
 * what it found. Written for Jarvis. It renders assistant-ui's tool-call part (see ``conversion.ts``), so the
 * thread passes it as the part's component.
 */
export const ToolStepRow: FC<{ args?: unknown }> = ({ args }) => {
  const step = stepFromArgs(args)
  const tone = stepTone(step)
  const Icon = TONE_ICON[tone]
  const [open, setOpen] = useState(false)
  const detailId = useId()

  return (
    <div data-slot="aui_tool-step" data-status={step.status} className="my-1 text-xs">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={detailId}
        onClick={() => setOpen((value) => !value)}
        className={cn(
          "border-foreground/10 hover:bg-muted/60 focus-visible:ring-ring/50 flex max-w-full items-center gap-1.5 rounded-md border px-2 py-1 text-left outline-none focus-visible:ring-2",
          TONE_COLOUR[tone],
        )}
      >
        <Icon className="size-3 shrink-0" aria-hidden />
        <span className="min-w-0 truncate">{step.label}</span>
        <ChevronRightIcon
          className={cn("size-3 shrink-0 transition-transform motion-reduce:transition-none", open && "rotate-90")}
          aria-hidden
        />
      </button>
      {open && (
        <div id={detailId} className="text-muted-foreground mt-1 ms-5 flex flex-col gap-0.5">
          {expandedLines(step).map((line, index) => (
            <span key={index} className="wrap-break-word">
              {line}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
