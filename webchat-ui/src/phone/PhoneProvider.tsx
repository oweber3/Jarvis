import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type FC, type ReactNode } from "react"
import { api, ApiError, type PhoneSnapshot, type Snapshot } from "@/api"
import { PhonePlayer, sharedAudioContext } from "@/phone/audioPlayer"
import { openMic } from "@/phone/micCapture"
import { phoneApi } from "@/phone/phoneApi"
import { readPrefs, storePrefs, type PhonePrefs } from "@/phone/storage"
import { VoiceSession, type VoicePorts, type VoiceView } from "@/phone/voiceSession"
import { JarvisProvider, useJarvis, type JarvisHooks } from "@/useJarvis"

// What only the phone has, around the shared page: the poll's `phone` part (confirmation, PC statistics, quick
// actions, voice availability and reply audio), the owner's switch, and the voice session. The chat itself is
// the shared JarvisProvider's.

export type PhoneView = {
  phone: PhoneSnapshot | null
  /** The PC's own assistant state, from the last poll. */
  pcState: string
  prefs: PhonePrefs
  setSpeakReplies: (on: boolean) => void
  /** Answer the desktop confirmation. Returns a plain sentence when it could not be answered, else null. */
  answer: (id: string, approve: boolean) => Promise<string | null>
  unpair: () => Promise<void>
}

export type VoiceControls = {
  view: VoiceView
  /** The level the orb follows right now, or null. */
  level: () => number | null
  tap: () => void
  setConversation: (on: boolean) => void
}

const PhoneContext = createContext<PhoneView | null>(null)
const VoiceContext = createContext<VoiceControls | null>(null)

export function usePhone(): PhoneView {
  const view = useContext(PhoneContext)
  if (!view) throw new Error("usePhone must be used inside PhoneProvider")
  return view
}

export function useVoice(): VoiceControls {
  const view = useContext(VoiceContext)
  if (!view) throw new Error("useVoice must be used inside PhoneProvider")
  return view
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

export const PhoneProvider: FC<{ onUnpaired: () => void; children: ReactNode }> = ({ onUnpaired, children }) => {
  const [phone, setPhone] = useState<PhoneSnapshot | null>(null)
  const [pcState, setPcState] = useState("asleep")
  const [prefs, setPrefs] = useState<PhonePrefs>(() => readPrefs())
  const prefsRef = useRef(prefs)
  prefsRef.current = prefs
  const sessionRef = useRef<VoiceSession | null>(null)

  const hooks = useMemo<JarvisHooks>(
    () => ({
      onSnapshot: (snapshot: Snapshot) => {
        setPcState(snapshot.state)
        setPhone(snapshot.phone ?? null)
        if (snapshot.phone) sessionRef.current?.audioChanged(snapshot.phone.voice.audio)
      },
      speakReplies: () => prefsRef.current.speakReplies,
      onSent: (queryId, speak) => {
        if (speak) void sessionRef.current?.speakReply(queryId)
      },
    }),
    [],
  )

  const setSpeakReplies = useCallback((on: boolean) => {
    const next = { ...prefsRef.current, speakReplies: on }
    storePrefs(next)
    setPrefs(next)
  }, [])

  const answer = useCallback(async (id: string, approve: boolean): Promise<string | null> => {
    try {
      await phoneApi.confirm(id, approve)
      return null
    } catch (error) {
      if (error instanceof ApiError && error.code === "not_pending") return "That was already answered."
      if (error instanceof ApiError && error.code === "confirm_disabled") return "Answering is turned off for phones."
      return "Jarvis could not be reached."
    }
  }, [])

  const unpair = useCallback(async () => {
    sessionRef.current?.stop()
    try {
      await phoneApi.unpair()
    } catch {
      /* the PC may be unreachable; this phone forgets its token either way */
    }
    onUnpaired()
  }, [onUnpaired])

  const view = useMemo<PhoneView>(
    () => ({ phone, pcState, prefs, setSpeakReplies, answer, unpair }),
    [phone, pcState, prefs, setSpeakReplies, answer, unpair],
  )

  return (
    <PhoneContext.Provider value={view}>
      <JarvisProvider hooks={hooks}>
        <VoiceBridge sessionRef={sessionRef}>{children}</VoiceBridge>
      </JarvisProvider>
    </PhoneContext.Provider>
  )
}

// Builds the voice session inside the shared provider, where the chat's actions are, and shares it.
const VoiceBridge: FC<{ sessionRef: { current: VoiceSession | null }; children: ReactNode }> = ({ sessionRef, children }) => {
  const { actions } = useJarvis()
  const { phone } = usePhone()
  const actionsRef = useRef(actions)
  actionsRef.current = actions
  const phoneRef = useRef(phone)
  phoneRef.current = phone
  const [voiceView, setVoiceView] = useState<VoiceView>({ phase: "idle", message: null, conversation: false })

  const session = useMemo(() => {
    let player: PhonePlayer | null = null
    const playerOf = () => (player ??= new PhonePlayer(sharedAudioContext()))
    const ports: VoicePorts = {
      unlock: () => playerOf().unlock(),
      openMic: () => openMic(sharedAudioContext()),
      availability: () => {
        if (!window.isSecureContext) return { available: false, reason: "no_https" }
        const voice = phoneRef.current?.voice
        return voice ? { available: voice.available, reason: voice.reason } : { available: true, reason: null }
      },
      sendAudio: (pcm, signal) => phoneApi.speak(pcm, signal),
      stopQuery: async () => void (await api.stop()),
      begin: (text) => actionsRef.current.beginSend(text),
      accepted: () => actionsRef.current.sendAccepted(),
      failed: () => actionsRef.current.sendFailed(),
      fetchAudio: (url) => phoneApi.replyAudio(url),
      play: (wav) => playerOf().play(wav),
      stopPlayback: () => player?.stop(),
      playerLevel: () => player?.level() ?? null,
      sleep,
    }
    return new VoiceSession(ports, setVoiceView)
  }, [])
  sessionRef.current = session

  // Backgrounding the page, or leaving it, lets go of the microphone and the audio.
  useEffect(() => {
    const leave = () => session.stop()
    const onVisibility = () => {
      if (document.visibilityState === "hidden") leave()
    }
    document.addEventListener("visibilitychange", onVisibility)
    window.addEventListener("pagehide", leave)
    return () => {
      document.removeEventListener("visibilitychange", onVisibility)
      window.removeEventListener("pagehide", leave)
      leave()
    }
  }, [session])

  const controls = useMemo<VoiceControls>(
    () => ({
      view: voiceView,
      level: () => session.level(),
      tap: () => session.tap(),
      setConversation: (on) => session.setConversation(on),
    }),
    [session, voiceView],
  )
  return <VoiceContext.Provider value={controls}>{children}</VoiceContext.Provider>
}
