import { useCallback, useState, type FC } from "react"
import { setCredentials } from "@/api"
import { PairScreen } from "@/phone/PairScreen"
import { PhoneProvider } from "@/phone/PhoneProvider"
import { PhoneShell } from "@/phone/PhoneShell"
import { readToken, storeToken } from "@/phone/storage"

// The phone app: pair first, then the chat. The device token lives in the browser's storage; the PC refusing it
// (this phone was removed there) sends the page back to pairing.

const REMOVED = "This phone was removed from Jarvis. Pair it again to carry on."

// The token must be in place before the first request of the page, so it is set when it is read.
let onRefused: (() => void) | null = null
function startingToken(): string | null {
  const token = readToken()
  if (token) setCredentials(token, () => onRefused?.())
  return token
}

export const PhoneApp: FC = () => {
  const [token, setToken] = useState<string | null>(startingToken)
  const [notice, setNotice] = useState<string | null>(null)

  const forget = useCallback((message: string | null) => {
    storeToken(null)
    setCredentials(null)
    setToken(null)
    setNotice(message)
  }, [])
  onRefused = () => forget(REMOVED)

  const paired = useCallback((fresh: string) => {
    storeToken(fresh)
    setCredentials(fresh, () => onRefused?.())
    setNotice(null)
    setToken(fresh)
  }, [])

  if (!token) return <PairScreen notice={notice} onPaired={paired} />
  return (
    <PhoneProvider onUnpaired={() => forget(null)}>
      <PhoneShell />
    </PhoneProvider>
  )
}
