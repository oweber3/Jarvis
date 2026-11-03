import { XIcon } from "lucide-react"
import type { FC } from "react"
import { cn } from "@/lib/utils"

// A short message from the page (could not reach the PC, already answered). It sits in the stack above the
// message box, or above voice mode's buttons, so it never covers the message box or the keyboard.

export const PhoneToast: FC<{ message: string; onDismiss: () => void; className?: string }> = ({ message, onDismiss, className }) => (
  <div
    role="alert"
    className={cn(
      "bg-popover text-popover-foreground border-border animate-in fade-in slide-in-from-bottom-1 flex items-center gap-2 rounded-xl border py-1 ps-4 pe-1 text-sm shadow-lg duration-150",
      className,
    )}
  >
    <span className="min-w-0 flex-1 break-words">{message}</span>
    <button
      type="button"
      aria-label="Dismiss"
      onClick={onDismiss}
      className="text-muted-foreground hover:text-foreground flex size-11 shrink-0 items-center justify-center rounded-lg"
    >
      <XIcon className="size-4" aria-hidden />
    </button>
  </div>
)
