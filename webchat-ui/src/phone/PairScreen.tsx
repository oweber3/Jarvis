import { useState, type FC, type FormEvent } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { phoneApi, describePairError } from "@/phone/phoneApi"

// Shown until this phone is paired: the six-digit code from the PC (the tray's Phone Access, or
// `python -m jarvis.remote pair`) and a name for this phone.

function guessName(): string {
  const agent = typeof navigator === "undefined" ? "" : navigator.userAgent
  if (/iPhone/i.test(agent)) return "iPhone"
  if (/iPad/i.test(agent)) return "iPad"
  if (/Android/i.test(agent)) return "Android phone"
  return "Phone"
}

export const PairScreen: FC<{ notice: string | null; onPaired: (token: string) => void }> = ({ notice, onPaired }) => {
  const [code, setCode] = useState("")
  const [name, setName] = useState(guessName)
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || code.length !== 6) return
    setBusy(true)
    setProblem(null)
    try {
      const paired = await phoneApi.pair(code, name.trim() || guessName())
      onPaired(paired.token)
    } catch (error) {
      setProblem(describePairError(error))
      setBusy(false)
    }
  }

  return (
    <main className="bg-background text-foreground flex min-h-dvh items-center justify-center px-4 py-8 pt-[max(2rem,env(safe-area-inset-top))]">
      <form onSubmit={submit} className="flex w-full max-w-sm flex-col gap-5" aria-label="Pair this phone">
        <div className="flex flex-col gap-1">
          <p className="text-primary text-xs font-medium tracking-[0.3em]">J.A.R.V.I.S.</p>
          <h1 className="text-2xl font-medium tracking-tight">Pair this phone</h1>
          <p className="text-muted-foreground text-sm">
            On your PC, open Jarvis in the tray, choose Phone Access and press Pair a phone. Type the code it shows.
          </p>
        </div>
        {notice && (
          <p role="status" className="bg-warning/10 text-warning border-warning/30 rounded-md border px-3 py-2 text-sm">
            {notice}
          </p>
        )}
        <label className="flex flex-col gap-1.5 text-sm">
          Code
          <Input
            value={code}
            onChange={(event) => setCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
            inputMode="numeric"
            autoComplete="one-time-code"
            autoFocus
            placeholder="123456"
            aria-label="Pairing code"
            className="h-12 text-center text-2xl tracking-[0.4em]"
          />
        </label>
        <label className="flex flex-col gap-1.5 text-sm">
          Name for this phone
          <Input
            value={name}
            onChange={(event) => setName(event.target.value.slice(0, 40))}
            aria-label="Phone name"
            className="h-11 text-base"
          />
        </label>
        {problem && (
          <p role="alert" className="text-destructive text-sm">
            {problem}
          </p>
        )}
        <Button type="submit" disabled={busy || code.length !== 6} className="h-12 text-base">
          {busy ? "Pairing..." : "Pair"}
        </Button>
      </form>
    </main>
  )
}
