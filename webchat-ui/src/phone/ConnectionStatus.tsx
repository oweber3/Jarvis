import type { FC } from "react"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { cn } from "@/lib/utils"
import { usePhone } from "@/phone/PhoneProvider"
import { connection, statusDetails } from "@/phone/statusLine"
import { useJarvis } from "@/useJarvis"

// The header's connection dot: green when the PC is connected and free, amber while Jarvis is busy or starting,
// red when the PC cannot be reached. A tap shows the details: the connection, who answers, and the PC's CPU and
// memory use.

const DOTS = { good: "bg-[var(--hud-success)]", warn: "bg-warning", bad: "bg-destructive" } as const

export const ConnectionStatus: FC = () => {
  const { reachable, ready, mode } = useJarvis()
  const { phone, busy } = usePhone()
  const input = { reachable, ready, busy, mode: mode?.mode, pc: phone?.pc }
  const dot = connection(input)

  return (
    <Popover>
      <PopoverTrigger
        aria-label={`${dot.label}. Show status`}
        className="hover:bg-muted flex size-11 shrink-0 items-center justify-center rounded-full"
      >
        <span aria-hidden className="relative flex size-3">
          {dot.tone === "warn" && <span className={cn("absolute inset-0 animate-ping rounded-full opacity-60", DOTS[dot.tone])} />}
          <span className={cn("relative size-3 rounded-full ring-2 ring-[var(--background)]", DOTS[dot.tone])} />
        </span>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-64 p-4">
        <dl aria-label="Status" className="flex flex-col gap-2 text-sm">
          {statusDetails(input).map((row, index) => (
            <div key={row.term} className="flex items-center justify-between gap-3">
              <dt className="text-muted-foreground">{row.term}</dt>
              <dd className="flex items-center gap-2 text-end tabular-nums">
                {index === 0 && <span aria-hidden className={cn("size-2 rounded-full", DOTS[dot.tone])} />}
                {row.text}
              </dd>
            </div>
          ))}
        </dl>
      </PopoverContent>
    </Popover>
  )
}
