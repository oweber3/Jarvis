import { useEffect, useState } from "react"
import { KeyboardTracker } from "@/phone/keyboard"

// Follows the visual viewport: tells whether the on-screen keyboard is up and keeps `--vvh` (the visible
// height in pixels) current, so the page's height is what the owner can actually see.

export function useKeyboardOpen(): boolean {
  const [open, setOpen] = useState(false)
  useEffect(() => {
    const viewport = window.visualViewport
    const tracker = new KeyboardTracker()
    const update = () => {
      const visible = viewport?.height ?? window.innerHeight
      document.documentElement.style.setProperty("--vvh", `${Math.round(visible)}px`)
      setOpen(tracker.update(window.innerHeight, visible, window.innerWidth))
      // iOS scrolls the page up to keep a focused field in view; the layout fills the visible area instead.
      if (viewport && viewport.offsetTop > 0) window.scrollTo(0, 0)
    }
    update()
    viewport?.addEventListener("resize", update)
    viewport?.addEventListener("scroll", update)
    window.addEventListener("resize", update)
    return () => {
      viewport?.removeEventListener("resize", update)
      viewport?.removeEventListener("scroll", update)
      window.removeEventListener("resize", update)
      document.documentElement.style.removeProperty("--vvh")
    }
  }, [])
  return open
}
