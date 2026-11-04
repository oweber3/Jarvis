import { XIcon } from "lucide-react"
import { Dialog as DialogPrimitive } from "radix-ui"
import { useRef, type FC } from "react"
import { ProjectsSidebar } from "@/components/ProjectsSidebar"

// The chats and projects, in a full-height drawer that slides in from the start edge over a backdrop. It closes
// when a chat is chosen (the shell watches the open chat), on a tap on the backdrop, on Esc, with its close
// button, and with a swipe towards the edge it came from.

const SWIPE_CLOSE_PX = 60

export const ChatsDrawer: FC<{ open: boolean; onOpenChange: (open: boolean) => void }> = ({ open, onOpenChange }) => {
  const swipe = useRef<{ x: number; y: number } | null>(null)
  const rtl = typeof document !== "undefined" && document.documentElement.dir === "rtl"

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay
          className="data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 fixed inset-0 z-50 bg-black/50"
        />
        <DialogPrimitive.Content
          aria-describedby={undefined}
          className="phone-drawer bg-card text-card-foreground border-border fixed inset-y-0 start-0 z-50 flex w-[min(20rem,85vw)] flex-col border-e shadow-2xl outline-none data-[state=open]:animate-in data-[state=open]:slide-in-from-left data-[state=closed]:animate-out data-[state=closed]:slide-out-to-left duration-200"
          onTouchStart={(event) => {
            const touch = event.touches[0]
            swipe.current = touch ? { x: touch.clientX, y: touch.clientY } : null
          }}
          onTouchEnd={(event) => {
            const start = swipe.current
            const touch = event.changedTouches[0]
            swipe.current = null
            if (!start || !touch) return
            const dx = (touch.clientX - start.x) * (rtl ? -1 : 1)
            const dy = touch.clientY - start.y
            if (dx < -SWIPE_CLOSE_PX && Math.abs(dx) > Math.abs(dy) * 1.5) onOpenChange(false)
          }}
        >
          <div className="flex h-14 shrink-0 items-center gap-2 ps-4 pe-2">
            <DialogPrimitive.Title className="flex-1 text-base font-semibold">Chats</DialogPrimitive.Title>
            <DialogPrimitive.Close
              aria-label="Close chats"
              className="text-muted-foreground hover:bg-muted hover:text-foreground flex size-11 items-center justify-center rounded-full"
            >
              <XIcon className="size-5" aria-hidden />
            </DialogPrimitive.Close>
          </div>
          <div className="min-h-0 flex-1">
            <ProjectsSidebar canClearAll={false} />
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}
