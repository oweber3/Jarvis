"""Voice for the phone: a spoken request in, the reply as one-shot audio out.

The phone sends 16 kHz mono PCM; the PC transcribes it with the Whisper model it already has, runs the text
through the normal request path and renders the reply with the voice Jarvis speaks with. Audio stays in memory
(never on disk), is offered to the one device that asked, once, and neither it nor any text is logged. See
``remote.spec.md``, Voice and Privacy of audio.
"""
from __future__ import annotations

import io
import queue
import secrets
import threading
import time
import wave
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional, Tuple

from ..debug import debug_log

RATE = 16000
MAX_RECORDING_SEC = 120
MAX_VOICE_BYTES = MAX_RECORDING_SEC * RATE * 2 + RATE * 2   # 120 s and one second of grace
MIN_VOICE_BYTES = int(0.3 * RATE) * 2
AUDIO_TTL_SEC = 120.0
MAX_CLIPS_PER_DEVICE = 8
MAX_STORE_BYTES = 32 * 1024 * 1024
MAX_SPEECH_CHARS = 3000
AUDIO_URL = "/api/audio/"

_SENTENCE_END = (".", "!", "?", "\n")


def wav_bytes(pcm: bytes, rate: int) -> bytes:
    """A mono, 16-bit WAV of the little-endian samples ``pcm``, built in memory."""
    out = io.BytesIO()
    with wave.open(out, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(int(rate))
        clip.writeframes(pcm)
    return out.getvalue()


class AudioStore:
    """Reply audio waiting to be fetched: in memory, one-shot, short-lived and bounded.

    A clip is found by a random 128-bit token and only by the device it was made for. Taking it removes it."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, ttl_sec: float = AUDIO_TTL_SEC,
                 max_per_device: int = MAX_CLIPS_PER_DEVICE, max_bytes: int = MAX_STORE_BYTES) -> None:
        self._clock = clock
        self._ttl = ttl_sec
        self._max_per_device = max_per_device
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._clips: "OrderedDict[str, Tuple[str, bytes, float]]" = OrderedDict()   # token -> (device, wav, expiry)

    def _sweep_locked(self) -> None:
        now = self._clock()
        for token in [t for t, (_d, _w, expiry) in self._clips.items() if expiry <= now]:
            del self._clips[token]

    def put(self, device_id: str, wav: bytes) -> Optional[str]:
        """Keep ``wav`` for ``device_id`` and return its token, or ``None`` when it is over the whole budget."""
        if len(wav) > self._max_bytes:
            return None
        with self._lock:
            self._sweep_locked()
            token = secrets.token_hex(16)
            self._clips[token] = (device_id, wav, self._clock() + self._ttl)
            own = [t for t, (d, _w, _e) in self._clips.items() if d == device_id]
            for old in own[:-self._max_per_device]:
                del self._clips[old]
            while sum(len(w) for _d, w, _e in self._clips.values()) > self._max_bytes and len(self._clips) > 1:
                self._clips.popitem(last=False)
            return token

    def take(self, token: str, device_id: str) -> Optional[bytes]:
        """The clip for ``token`` if it was made for ``device_id`` and has not expired, removing it; else ``None``."""
        with self._lock:
            self._sweep_locked()
            found = self._clips.get(token)
            if found is None or found[0] != device_id:
                return None
            del self._clips[token]
            return found[1]


class _Tracker:
    """Follows one request that asks for its reply to be spoken to the phone."""

    def __init__(self, voice: "PhoneVoice", device_id: str) -> None:
        self._voice, self._device = voice, device_id

    def accepted(self, query_id: int) -> None:
        self._voice._expect(self._device, query_id)

    def finished(self, query_id: int, status: str, reply: Optional[str]) -> None:
        self._voice._finished(self._device, query_id, status, reply)


class PhoneVoice:
    """Speech for the phone. ``backend`` is a ``DaemonBackend`` or a test fake; ``hub`` is the shared ``ChatHub``.

    ``inline`` renders on the calling thread (for tests); otherwise one worker thread renders, one reply at a time."""

    def __init__(self, backend, hub, *, inline: bool = False, poke: Optional[Callable[[], None]] = None,
                 clock: Callable[[], float] = time.monotonic, ttl_sec: float = AUDIO_TTL_SEC) -> None:
        self._backend = backend
        self._hub = hub
        self._inline = inline
        self._poke = poke if poke is not None else hub.poke
        self._clock = clock
        self._ttl = ttl_sec
        self.store = AudioStore(clock=clock, ttl_sec=ttl_sec)
        self._lock = threading.Lock()
        self._entries: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self._listening = threading.Lock()   # one transcription at a time
        self._jobs: "queue.Queue" = queue.Queue()
        self._worker: Optional[threading.Thread] = None

    # -- availability -----------------------------------------------------------------

    def availability(self, *, secure: bool) -> Dict[str, Any]:
        """Whether a spoken request can work now, and if not why (``no_https``, or the speech model's reason)."""
        if not secure:
            return {"available": False, "reason": "no_https"}
        reason = self._backend.voice_reason()
        return {"available": reason is None, "reason": reason}

    # -- a spoken request ---------------------------------------------------------------

    def handle_audio(self, device_id: str, pcm: bytes, *, secure: bool) -> Tuple[int, Dict[str, Any]]:
        """Transcribe ``pcm`` and send the words as a spoken phone request: ``(status, payload)``.

        The words appear only in this answer and in the redacted turn the chat stores."""
        if len(pcm) > MAX_VOICE_BYTES:
            return 413, {"error": "too_large"}
        if len(pcm) % 2:
            return 400, {"error": "bad_audio"}
        if len(pcm) < MIN_VOICE_BYTES:
            return 400, {"error": "too_short"}
        available = self.availability(secure=secure)
        if not available["available"]:
            debug_log(f"remote: voice refused ({available['reason']})", "remote")
            return 503, {"error": available["reason"]}
        if self._backend.is_busy() or not self._listening.acquire(blocking=False):
            debug_log("remote: voice refused (busy)", "remote")
            return 409, {"error": "busy"}
        try:
            seconds = len(pcm) / (2 * RATE)
            debug_log(f"remote: voice upload of {len(pcm)} bytes ({seconds:.1f}s)", "remote")
            started = time.monotonic()
            text = self._transcribe(pcm)
            debug_log(f"remote: transcribed {len(text)} characters in {time.monotonic() - started:.1f}s", "remote")
        finally:
            self._listening.release()
        if not text:
            return 200, {"status": "nothing_heard", "text": ""}
        tracker = self.track(device_id)
        result = self._hub.submit(text, origin="phone", source="voice", on_finish=tracker.finished)
        if result.status == "accepted":
            tracker.accepted(result.query_id)
            return 202, {"query_id": result.query_id, "text": text, "status": "accepted"}
        return (409, {"error": "busy"}) if result.status == "busy" else (503, {"error": "unavailable"})

    def _transcribe(self, pcm: bytes) -> str:
        import numpy as np
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        try:
            return (self._backend.transcribe(samples) or "").strip()
        except Exception as exc:
            debug_log(f"remote: transcription failed: {type(exc).__name__}", "remote")
            return ""

    # -- reply audio ----------------------------------------------------------------------

    def track(self, device_id: str) -> _Tracker:
        """A tracker whose ``finished`` goes to ``ChatHub.submit(on_finish=...)`` and whose ``accepted`` is
        called with the query id the hub returned."""
        return _Tracker(self, device_id)

    def _expect(self, device_id: str, query_id: int) -> None:
        with self._lock:
            self._sweep_locked()
            self._entries.setdefault((device_id, query_id), {"status": "pending", "token": None,
                                                              "created": self._clock()})

    def _finished(self, device_id: str, query_id: int, status: str, reply: Optional[str]) -> None:
        text = self._speakable(reply) if status == "done" else ""
        if not text:
            self._set(device_id, query_id, "none")
            return
        self._set(device_id, query_id, "pending")
        if self._inline:
            self._render(device_id, query_id, text)
        else:
            self._ensure_worker()
            self._jobs.put((device_id, query_id, text))

    @staticmethod
    def _speakable(reply: Optional[str]) -> str:
        text = (reply or "").strip()
        if len(text) <= MAX_SPEECH_CHARS:
            return text
        cut = max(text.rfind(mark, 0, MAX_SPEECH_CHARS) for mark in _SENTENCE_END)
        return text[:cut + 1 if cut > 0 else MAX_SPEECH_CHARS].strip()

    def _render(self, device_id: str, query_id: int, text: str) -> None:
        started = time.monotonic()
        try:
            speech = self._backend.render_speech(text)
            token = None
            if speech is not None and speech[0]:
                token = self.store.put(device_id, wav_bytes(speech[0], speech[1]))
        except Exception as exc:
            debug_log(f"remote: could not render a reply: {type(exc).__name__}", "remote")
            token = None
        if token is None:
            self._set(device_id, query_id, "none")
        else:
            self._set(device_id, query_id, "ready", token)
            debug_log(f"remote: reply audio ready for {len(text)} characters in "
                      f"{time.monotonic() - started:.1f}s", "remote")

    def _set(self, device_id: str, query_id: int, status: str, token: Optional[str] = None) -> None:
        with self._lock:
            entry = self._entries.setdefault((device_id, query_id), {"created": self._clock()})
            entry["status"], entry["token"] = status, token
        self._poke()

    def _sweep_locked(self) -> None:
        now = self._clock()
        for key in [k for k, e in self._entries.items() if now - e["created"] > self._ttl]:
            del self._entries[key]

    def audio_status(self, device_id: str) -> Dict[str, Dict[str, Any]]:
        """This device's reply audio by query id: ``pending``, ``ready`` (with the one-shot ``url``) or ``none``."""
        with self._lock:
            self._sweep_locked()
            found = {}
            for (device, query_id), entry in self._entries.items():
                if device != device_id:
                    continue
                item: Dict[str, Any] = {"status": entry["status"]}
                if entry["status"] == "ready":
                    item["url"] = AUDIO_URL + entry["token"]
                found[str(query_id)] = item
            return found

    def watch_value(self) -> tuple:
        """A comparable value that changes when any audio status does (for ``ChatHub.watch``)."""
        with self._lock:
            return tuple(sorted((k, e["status"]) for k, e in self._entries.items()))

    def fetch(self, token: str, device_id: str) -> Optional[bytes]:
        """The reply audio for ``token``, once, for the device it was made for."""
        clip = self.store.take(token, device_id)
        if clip is None:
            return None
        with self._lock:
            for entry in self._entries.values():
                if entry["token"] == token:
                    entry["status"], entry["token"] = "none", None
        debug_log(f"remote: reply audio of {len(clip)} bytes fetched", "remote")
        return clip

    # -- the render worker --------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None:
                return
            self._worker = threading.Thread(target=self._work, name="jarvis-remote-voice", daemon=True)
            self._worker.start()

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            self._render(*job)

    def shutdown(self) -> None:
        with self._lock:
            worker, self._worker = self._worker, None
        if worker is not None:
            self._jobs.put(None)
            worker.join(timeout=2.0)
