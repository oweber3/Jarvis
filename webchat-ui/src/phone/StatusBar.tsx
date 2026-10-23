import type { FC } from "react"
import { cn } from "@/lib/utils"
import { usePhone } from "@/phone/PhoneProvider"
import { statusParts } from "@/phone/statusLine"
import { useJarvis } from "@/useJarvis"

const TONES = { good: "text-[var(--hud-success)]", warn: "text-warning", muted: "text-muted-foreground" } as const

export const StatusLine: FC = () => {
  const { reachable, ready, mode } = useJarvis()
  const { phone } = usePhone()
  const parts = statusParts({ reachable, ready, mode: mode?.mode, pc: phone?.pc })
  return (
    <p role="status" aria-label="Status" className="flex flex-wrap items-center justify-center gap-x-2 px-3 py-1 text-xs">
      {parts.map((part, index) => (
        <span key={part.text} className="flex items-center gap-2">
          {index > 0 && <span aria-hidden className="text-muted-foreground/50">·</span>}
          <span className={cn(TONES[part.tone])}>{part.text}</span>
        </span>
      ))}
    </p>
  )
}
