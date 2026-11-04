import { useId, type FC } from "react"
import { cn } from "@/lib/utils"

// Jarvis's mark: the cyan core the desktop chat draws for its contact (desktop_app/chat_window.py, JarvisOrb), as
// an SVG in the page's palette, so it follows light and dark. Decorative: it carries no name of its own.

export const JarvisMark: FC<{ className?: string }> = ({ className }) => {
  const glow = useId()
  return (
    <svg viewBox="0 0 100 100" aria-hidden focusable="false" className={cn("shrink-0", className)}>
      <defs>
        <radialGradient id={glow}>
          <stop offset="0%" stopColor="var(--hud-accent-primary)" stopOpacity="0.32" />
          <stop offset="100%" stopColor="var(--hud-accent-primary)" stopOpacity="0" />
        </radialGradient>
      </defs>
      <circle cx="50" cy="50" r="50" fill={`url(#${glow})`} />
      <circle cx="50" cy="50" r="34" fill="none" stroke="var(--hud-accent-deep)" strokeWidth="2.5" />
      <path
        d="M 75.4 38.2 A 28 28 0 0 0 24.6 38.2 M 24.6 61.8 A 28 28 0 0 0 75.4 61.8"
        fill="none"
        stroke="var(--hud-accent-secondary)"
        strokeWidth="4"
        strokeLinecap="round"
      />
      <g stroke="var(--hud-accent-secondary)" strokeWidth="5" strokeLinecap="round">
        <line x1="38" y1="45" x2="38" y2="55" />
        <line x1="46" y1="38" x2="46" y2="62" />
        <line x1="54" y1="34" x2="54" y2="66" />
        <line x1="62" y1="42" x2="62" y2="58" />
      </g>
    </svg>
  )
}
