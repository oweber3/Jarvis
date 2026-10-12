import { useEffect, useState } from "react"
import { api, type SearchHit } from "@/api"

const DEBOUNCE_MS = 250
export const MIN_SEARCH_CHARS = 2

// Chats whose messages contain ``query``, asked of the server once typing pauses. A newer query
// supersedes an older one, and a failed search simply shows no message results.
export function useMessageSearch(query: string): { hits: SearchHit[]; searching: boolean } {
  const needle = query.trim()
  const [state, setState] = useState<{ needle: string; hits: SearchHit[] }>({ needle: "", hits: [] })
  const wanted = needle.length >= MIN_SEARCH_CHARS

  useEffect(() => {
    if (!wanted) return
    const abort = new AbortController()
    const timer = window.setTimeout(() => {
      api
        .search(needle, abort.signal)
        .then((found) => setState({ needle, hits: found.results }))
        .catch(() => {
          if (!abort.signal.aborted) setState({ needle, hits: [] })
        })
    }, DEBOUNCE_MS)
    return () => {
      window.clearTimeout(timer)
      abort.abort()
    }
  }, [needle, wanted])

  return { hits: wanted && state.needle === needle ? state.hits : [], searching: wanted && state.needle !== needle }
}
