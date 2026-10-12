import { useState, type FC, type ReactNode } from "react"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { confirmWordTyped } from "@/confirm"

// A modal question before something that cannot be undone. Esc, the close button and Cancel all leave
// without doing it. With ``requireWord`` the owner must also type that word before the button works.
export const ConfirmDialog: FC<{
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description: ReactNode
  confirmLabel: string
  requireWord?: string
  onConfirm: () => void | Promise<void>
}> = ({ open, onOpenChange, title, description, confirmLabel, requireWord, onConfirm }) => {
  const [typed, setTyped] = useState("")
  const [working, setWorking] = useState(false)
  const allowed = !working && (requireWord === undefined || confirmWordTyped(typed, requireWord))

  const close = (next: boolean) => {
    if (!next) setTyped("")
    onOpenChange(next)
  }
  const confirm = async () => {
    if (!allowed) return
    setWorking(true)
    try {
      await onConfirm()
    } finally {
      setWorking(false)
      close(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription asChild>
            <div className="space-y-2">{description}</div>
          </DialogDescription>
        </DialogHeader>
        {requireWord !== undefined && (
          <label className="flex flex-col gap-1.5 text-sm">
            <span>
              Type <strong className="font-semibold">{requireWord}</strong> to confirm
            </span>
            <Input
              value={typed}
              autoComplete="off"
              onChange={(event) => setTyped(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault()
                  void confirm()
                }
              }}
            />
          </label>
        )}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => close(false)}>
            Cancel
          </Button>
          <Button type="button" variant="destructive" disabled={!allowed} onClick={() => void confirm()}>
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
