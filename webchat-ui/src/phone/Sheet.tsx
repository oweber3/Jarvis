import { XIcon } from "lucide-react"
import { Dialog as DialogPrimitive } from "radix-ui"
import type { FC, ReactNode } from "react"
import { cn } from "@/lib/utils"

// A bottom sheet: a modal panel that rises from the bottom edge, above the safe area, with a title and a close
// button. Esc, a tap on the backdrop or the close button closes it, and focus returns where it was.

export type SheetProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description?: string
  children: ReactNode
  className?: string
}

export const Sheet: FC<SheetProps> = ({ open, onOpenChange, title, description, children, className }) => (
  <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 fixed inset-0 z-50 bg-black/50" />
      <DialogPrimitive.Content
        {...(description ? {} : { "aria-describedby": undefined })}
        className={cn(
          "phone-sheet bg-popover text-popover-foreground border-border fixed inset-x-0 bottom-0 z-50 mx-auto flex w-full max-w-lg flex-col rounded-t-2xl border-t shadow-2xl outline-none",
          "data-[state=open]:animate-in data-[state=open]:slide-in-from-bottom data-[state=closed]:animate-out data-[state=closed]:slide-out-to-bottom duration-200",
          className,
        )}
      >
        <div aria-hidden className="bg-muted-foreground/30 mx-auto mt-2 h-1 w-10 shrink-0 rounded-full" />
        <div className="flex shrink-0 items-center gap-2 ps-5 pe-2 pt-1">
          <div className="flex min-w-0 flex-1 flex-col">
            <DialogPrimitive.Title className="text-base font-semibold">{title}</DialogPrimitive.Title>
            {description && (
              <DialogPrimitive.Description className="text-muted-foreground text-xs">{description}</DialogPrimitive.Description>
            )}
          </div>
          <DialogPrimitive.Close
            aria-label="Close"
            className="text-muted-foreground hover:bg-muted hover:text-foreground flex size-11 items-center justify-center rounded-full"
          >
            <XIcon className="size-5" aria-hidden />
          </DialogPrimitive.Close>
        </div>
        <div className="phone-sheet-body min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 pt-2">{children}</div>
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  </DialogPrimitive.Root>
)
