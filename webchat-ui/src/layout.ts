import { useSyncExternalStore } from "react"

// Below Tailwind's `md` breakpoint the chat list is an overlay on top of the chat instead of a column beside it.
export const NARROW_QUERY = "(max-width: 767.98px)"

export function isNarrow(win: Pick<Window, "matchMedia"> = window): boolean {
  return win.matchMedia(NARROW_QUERY).matches
}

function subscribe(onChange: () => void): () => void {
  const query = window.matchMedia(NARROW_QUERY)
  query.addEventListener("change", onChange)
  return () => query.removeEventListener("change", onChange)
}

export function useNarrow(): boolean {
  return useSyncExternalStore(subscribe, () => isNarrow())
}
