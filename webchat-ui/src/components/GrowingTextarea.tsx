import { forwardRef, useCallback, useEffect, useLayoutEffect, useRef, type ComponentPropsWithoutRef } from "react"

// The message box's text area, which grows with what is typed up to its CSS max-height and then scrolls. It measures
// itself only when its text changes and when the window is resized, never on a redraw that changed nothing it
// shows: measuring forces the browser to lay out the page, which during a load would happen on every update.

type Props = ComponentPropsWithoutRef<"textarea"> & { onHeightChange?: (height: number) => void }

export const GrowingTextarea = forwardRef<HTMLTextAreaElement, Props>(function GrowingTextarea(
  { value, onHeightChange, ...props },
  forwarded,
) {
  const element = useRef<HTMLTextAreaElement | null>(null)
  const fit = useCallback(() => {
    const area = element.current
    if (!area) return
    area.style.height = "auto"
    area.style.height = `${area.scrollHeight}px`
    onHeightChange?.(area.offsetHeight)
  }, [onHeightChange])

  useLayoutEffect(fit, [value, fit])

  useEffect(() => {
    let frame = 0
    const onResize = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(fit)
    }
    window.addEventListener("resize", onResize)
    return () => {
      cancelAnimationFrame(frame)
      window.removeEventListener("resize", onResize)
    }
  }, [fit])

  return (
    <textarea
      {...props}
      value={value}
      ref={(area) => {
        element.current = area
        if (typeof forwarded === "function") forwarded(area)
        else if (forwarded) forwarded.current = area
      }}
    />
  )
})
