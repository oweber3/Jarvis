"""
Voice Listener - Main orchestrator for voice capture and processing.

Coordinates audio capture, speech recognition, echo detection, and state management.
"""

from __future__ import annotations
import functools
import os
import threading
import time
import queue
import sys
import platform
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING, Any, Callable, Literal
from datetime import datetime

from rapidfuzz import fuzz
from contextlib import contextmanager

from .echo_detection import EchoDetector
from .state_manager import StateManager, ListeningState
from ..utils.audio_lock import portaudio_lock
from ..utils.audio_capture import mono_capture, open_input_stream, resolve_input_device
from .wake_detection import is_wake_word_detected, extract_query_after_wake, is_stop_command, is_bare_wake_word
from .transcript_buffer import TranscriptBuffer
from .latency import TurnLatency
from .speaker_verification import Verdict, create_verifier
from .intent_judge import (
    IntentJudge,
    _is_low_power_mode_enabled,
    create_intent_judge,
    warm_up_chat_model,
)
from ..assistant_state import AssistantState, set_state
from ..bridge import modes as reply_modes
from ..debug import debug_log
from ..llm import get_embedding_backend
from ..utils.location import is_location_available

if TYPE_CHECKING:
    from ..memory.db import Database
    from ..memory.conversation import DialogueMemory


@dataclass(frozen=True)
class LowConfidenceEvent:
    """A rejected Whisper segment, available in memory to listener consumers."""

    confidence: float
    transcript: str
    reason: Literal["low_confidence"] = "low_confidence"


def is_whisper_hallucination(no_speech_prob: float, threshold: float) -> bool:
    """Shared Whisper no-speech gate.

    Whisper can report high `avg_logprob` confidence on hallucinated phrases
    when the audio is silent or noise. `no_speech_prob` is an independent
    signal and must be checked first. Used by both the faster-whisper path
    (`_filter_noisy_segments`) and the MLX path (`_transcribe_audio`) so
    both backends apply identical policy.
    """
    return no_speech_prob >= threshold


@dataclass(frozen=True)
class _TranscriptionJob:
    audio: Any
    start_time: float
    end_time: float
    speech_end_time: float
    energy: float
    dictation_generation: int
    captured_during_tts: bool
    captured_tts_start_time: float


@dataclass(frozen=True)
class _TranscriptionResult:
    text: str
    language: Optional[str]
    low_confidence_events: tuple[LowConfidenceEvent, ...]
    start_time: float
    end_time: float
    speech_end_time: float
    energy: float
    dictation_generation: int
    captured_during_tts: bool
    captured_tts_start_time: float
    speaker_verdict: Verdict = Verdict.UNKNOWN

# Audio processing imports (optional)
try:
    import sounddevice as sd
    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='pkg_resources is deprecated',
                                category=UserWarning, module='webrtcvad')
        import webrtcvad
    import numpy as np
except ImportError as e:
    sd = None
    webrtcvad = None
    np = None
    # Log import error for debugging
    print(f"  ⚠️  Audio import error: {e}", flush=True)
    print("     This may indicate PortAudio is not found", flush=True)
    import sys as _sys
    if _sys.platform == 'linux':
        print("     On Linux, ensure PortAudio is installed: sudo apt install libportaudio2", flush=True)
    del _sys
except OSError as e:
    # PortAudio loading errors appear as OSError
    sd = None
    webrtcvad = None
    np = None
    print(f"  ❌ PortAudio initialisation failed: {e}", flush=True)
    print("     Please reinstall the application or check audio drivers", flush=True)
    import sys as _sys
    if _sys.platform == 'linux':
        print("     On Linux, ensure PortAudio is installed: sudo apt install libportaudio2", flush=True)
    del _sys

# Whisper backend imports - try MLX first on Apple Silicon, fall back to faster-whisper
MLX_WHISPER_AVAILABLE = False
FASTER_WHISPER_AVAILABLE = False

def _is_apple_silicon() -> bool:
    """Check if running on Apple Silicon Mac."""
    return sys.platform == "darwin" and platform.machine() == "arm64"


def _get_mic_permission_hint() -> str:
    """Return platform-appropriate microphone permission guidance."""
    if sys.platform == 'win32':
        return "Windows Settings > Privacy > Microphone > Allow apps to access"
    elif sys.platform == 'darwin':
        return "System Settings > Privacy & Security > Microphone"
    else:
        return "`pactl list sources` or audio settings for your desktop environment"

def _resample(audio, src_rate: int, dst_rate: int):
    """Resample a 1-D float32 numpy array from *src_rate* to *dst_rate*.

    Uses linear interpolation — fast and good enough for speech going into Whisper.
    """
    if src_rate == dst_rate or np is None:
        return audio
    ratio = dst_rate / src_rate
    n_out = int(len(audio) * ratio)
    indices = np.arange(n_out) / ratio
    return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)


def _setup_nvidia_dll_path() -> None:
    """Add NVIDIA CUDA DLL directories to PATH on Windows.

    The pip packages nvidia-cublas-cu12 and nvidia-cudnn-cu12 install DLLs
    under site-packages/nvidia/*/bin/ which isn't on PATH by default.
    PyInstaller bundles place them in {app}/cuda/. This function finds
    both locations and prepends them to PATH so ctypes.CDLL can find them.
    """
    import os

    dirs_to_add = []

    # 1. Check for NVIDIA pip packages in site-packages
    try:
        import nvidia.cublas  # type: ignore[import-untyped]
        for pkg_path in nvidia.cublas.__path__:
            bin_dir = os.path.join(pkg_path, "bin")
            if os.path.isdir(bin_dir):
                dirs_to_add.append(bin_dir)
    except (ImportError, AttributeError):
        pass

    try:
        import nvidia.cudnn  # type: ignore[import-untyped]
        for pkg_path in nvidia.cudnn.__path__:
            bin_dir = os.path.join(pkg_path, "bin")
            if os.path.isdir(bin_dir):
                dirs_to_add.append(bin_dir)
    except (ImportError, AttributeError):
        pass

    # 2. Check for CUDA DLLs in app directory (installed by install_cuda.ps1)
    # For frozen apps: check next to the executable (not _MEIPASS, since
    # CUDA libs are downloaded post-install, not bundled in the archive)
    if getattr(sys, "frozen", False):
        app_dir = os.path.dirname(sys.executable)
    else:
        app_dir = None

    if app_dir:
        cuda_dir = os.path.join(app_dir, "cuda")
        if os.path.isdir(cuda_dir):
            dirs_to_add.append(cuda_dir)

    # 3. Register DLL directories (must happen before ctypes.CDLL probes)
    # Use both os.add_dll_directory (for ctypes.CDLL) and PATH (for
    # subprocess/child processes). On Windows, PATH changes after process
    # start don't affect ctypes.CDLL search — add_dll_directory is needed.
    if dirs_to_add:
        current_path = os.environ.get("PATH", "")
        new_entries = os.pathsep.join(dirs_to_add)
        os.environ["PATH"] = new_entries + os.pathsep + current_path
        for d in dirs_to_add:
            try:
                os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass
            debug_log(f"added NVIDIA DLL path: {d}", "voice")


@functools.lru_cache(maxsize=None)
def _probe_cuda_available() -> tuple[bool, list[str]]:
    """Probe cuBLAS + cuDNN availability once per process and cache the result.

    The version ranges intentionally span more than the currently pinned
    versions in `installer/windows/install_cuda.ps1` (`cublas64_12.dll`,
    `cudnn_ops64_9.dll`) so a future installer bump doesn't silently fall
    back to CPU until this probe is updated too. A bump outside the
    existing range still requires widening these ranges — the relationship
    is by convention, not enforced.

    Cached because DLLs don't appear or disappear while the process is
    running, and the scan does up to 18 `LoadLibrary` calls on a miss.
    """
    _setup_nvidia_dll_path()

    missing_libs: list[str] = []
    cublas_found = False
    cudnn_found = False
    try:
        import ctypes

        for ver in range(20, 10, -1):
            try:
                ctypes.CDLL(f"cublas64_{ver}.dll")
                cublas_found = True
                debug_log(f"cuBLAS found (cublas64_{ver}.dll)", "voice")
                break
            except OSError:
                continue
        if not cublas_found:
            missing_libs.append("cuBLAS")

        for ver in range(15, 7, -1):
            try:
                ctypes.CDLL(f"cudnn_ops64_{ver}.dll")
                cudnn_found = True
                debug_log(f"cuDNN found (cudnn_ops64_{ver}.dll)", "voice")
                break
            except OSError:
                continue
        if not cudnn_found:
            missing_libs.append("cuDNN")
    except Exception as e:
        debug_log(f"CUDA library probe failed: {e}", "voice")

    return cublas_found and cudnn_found, missing_libs


def _probe_windows_cuda_libraries(device: str) -> tuple[str, list[str]]:
    """Return the device to use and any missing CUDA lib names.

    Short-circuits on non-Windows or non-CUDA device strings. Otherwise
    delegates to the cached `_probe_cuda_available()` so the expensive DLL
    scan only runs once per process lifetime.
    """
    if sys.platform != "win32" or device not in ("auto", "cuda"):
        return device, []

    available, missing_libs = _probe_cuda_available()
    if not available:
        return "cpu", missing_libs
    return device, []


def _print_cuda_unavailable_hint(missing_libs: list[str]) -> None:
    """Print the user-facing CUDA-missing message and recovery hint.

    The hint deliberately points at the tray action, not at "reinstall the
    app". The Inno Setup task only fires once and skips on stale marker
    files, so reinstalling without first deleting `{app}\\cuda` rarely
    fixes the underlying problem. The tray action re-runs install_cuda.ps1
    directly with UAC, which is the actual recovery path.
    """
    debug_log(f"CUDA libraries missing: {missing_libs}, forcing CPU mode", "voice")
    print("  ℹ️  CUDA not available, using CPU mode", flush=True)
    if missing_libs:
        print(f"     Missing: {', '.join(missing_libs)}", flush=True)
    print(
        "  💡 For GPU acceleration, click 'Reinstall GPU libraries' in the Jarvis tray menu",
        flush=True,
    )


try:
    if _is_apple_silicon():
        import mlx_whisper
        MLX_WHISPER_AVAILABLE = True
except Exception:
    mlx_whisper = None

# faster_whisper (with ctranslate2 and PyAV, about 0.2 s) is imported when the model is loaded on
# the listener thread, not when this module is imported; see _faster_whisper_model_class.
WhisperModel = None
try:
    import importlib.util as _importlib_util
    FASTER_WHISPER_AVAILABLE = _importlib_util.find_spec("faster_whisper") is not None
except Exception:
    FASTER_WHISPER_AVAILABLE = False


def _faster_whisper_model_class():
    """``faster_whisper.WhisperModel``, imported on first use, or None when it cannot be imported."""
    global WhisperModel, FASTER_WHISPER_AVAILABLE
    if WhisperModel is None and FASTER_WHISPER_AVAILABLE:
        try:
            from faster_whisper import WhisperModel as model_class
            WhisperModel = model_class
        except Exception:
            # Catch broad: the faster-whisper import chain can raise ValueError
            # (e.g. "psutil.__spec__ is not set") in some environments.
            FASTER_WHISPER_AVAILABLE = False
    return WhisperModel if FASTER_WHISPER_AVAILABLE else None


def _is_faster_whisper_turbo_supported() -> bool:
    """Check if the installed faster-whisper supports the large-v3-turbo model."""
    try:
        import faster_whisper
        from packaging.version import Version
        return Version(faster_whisper.__version__) >= Version("1.1.0")
    except Exception:
        return False


def _get_mlx_model_repo(model_name: str) -> str:
    """Get the MLX Community HuggingFace repo for a Whisper model."""
    # Map standard model names to MLX Community repos
    model_map = {
        "tiny": "mlx-community/whisper-tiny-mlx",
        "tiny.en": "mlx-community/whisper-tiny.en-mlx",
        "base": "mlx-community/whisper-base-mlx",
        "base.en": "mlx-community/whisper-base.en-mlx",
        "small": "mlx-community/whisper-small-mlx",
        "small.en": "mlx-community/whisper-small.en-mlx",
        "medium": "mlx-community/whisper-medium-mlx",
        "medium.en": "mlx-community/whisper-medium.en-mlx",
        "large": "mlx-community/whisper-large-v3-mlx",
        "large-v2": "mlx-community/whisper-large-v2-mlx",
        "large-v3": "mlx-community/whisper-large-v3-mlx",
        "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    }
    return model_map.get(model_name, f"mlx-community/whisper-{model_name}-mlx")


def _clear_corrupted_whisper_cache(error_message: str) -> bool:
    """Clear a corrupted Whisper model cache directory.

    Parses the CTranslate2 error message to find the snapshot directory,
    then deletes the parent ``models--`` directory so the model can be
    re-downloaded cleanly (including blobs that may also be corrupt).

    Returns ``True`` if a cache directory was found and deleted.
    """
    import re
    import shutil

    # CTranslate2 error format:
    #   "Unable to open file 'model.bin' in model '/path/to/snapshots/hash'"
    match = re.search(
        r"unable to open file\s+'[^']+'\s+in model\s+'([^']+)'",
        error_message,
        re.IGNORECASE,
    )
    if not match:
        debug_log("could not parse cache path from error message", "voice")
        return False

    snapshot_path = match.group(1)

    # Walk up to the models-- directory
    # snapshot_path is e.g. .../models--Org--Name/snapshots/<hash>
    # We want to delete .../models--Org--Name entirely
    from pathlib import Path
    path = Path(snapshot_path)
    model_dir = None
    for parent in [path] + list(path.parents):
        if parent.name.startswith("models--"):
            model_dir = parent
            break

    if model_dir is None or not model_dir.is_dir():
        debug_log(f"could not locate models-- cache directory from: {snapshot_path}", "voice")
        return False

    try:
        shutil.rmtree(model_dir)
        debug_log(f"cleared corrupted Whisper cache: {model_dir}", "voice")
        return True
    except OSError as e:
        debug_log(f"failed to clear corrupted cache: {e}", "voice")
        return False



@contextmanager
def _serialised_stream(stream):
    """Like ``with stream:`` but with lifecycle calls under portaudio_lock.

    sounddevice's context manager calls start() on enter and stop()/close()
    on exit; those are the thread-unsafe PortAudio lifecycle operations that
    must be serialised process-wide (see jarvis.utils.audio_lock).
    """
    with portaudio_lock:
        stream.start()
    try:
        yield stream
    finally:
        with portaudio_lock:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass


class VoiceListener(threading.Thread):
    """Main voice listening thread that orchestrates all voice processing."""

    # Diagnostics state; class defaults keep partially constructed listeners valid.
    _whisper_backend: Optional[str] = None  # "mlx" or "faster-whisper"
    _whisper_device: Optional[str] = None  # "cpu" or "cuda" (resolved from CTranslate2)
    _whisper_compute: Optional[str] = None  # CTranslate2 compute type that loaded
    _last_voice_frame_time: float = 0.0  # Wall time of the latest voiced VAD frame
    _turn: Optional[TurnLatency] = None  # Latency marks for the turn being processed
    _speaker_verifier: Optional[Any] = None  # Set when speaker verification is on and enrolled
    _barge_in_fired = False  # TTS was ducked for the reply being spoken
    _barge_in_deadline = 0.0
    _barge_in_voiced_frames = 0
    _barge_in_next_check = 0
    _barge_in_passes = 0  # consecutive windows that scored as the owner
    _BARGE_IN_CONFIRMATIONS = 2  # windows in a row, so one lucky mixture cannot duck Jarvis

    # Reply generation runs on one serial worker so the listener keeps hearing.
    _reply_executor: Optional[ThreadPoolExecutor] = None
    _pending_replies: tuple = ()  # Cancel flags of replies queued or being generated
    _reply_lock = threading.Lock()
    _engagement_lock = threading.RLock()  # Engagement starts and ends on listener and reply threads

    def __init__(self, db: "Database", cfg, tts: Optional[Any],
                 dialogue_memory: "DialogueMemory", *,
                 on_low_confidence: Optional[Callable[[LowConfidenceEvent], None]] = None):
        """
        Initialise voice listener.

        Args:
            db: Database instance for storage
            cfg: Configuration object
            tts: Text-to-speech engine (optional)
            dialogue_memory: Dialogue memory instance
            on_low_confidence: Optional per-segment rejection callback. Runs
                synchronously on the listener thread and must not block;
                consumers should enqueue work for their own thread if needed.
        """
        super().__init__(daemon=True)

        self.db = db
        self.cfg = cfg
        self.tts = tts
        self.dialogue_memory = dialogue_memory
        self.on_low_confidence = on_low_confidence
        self._should_stop = False
        self._dictation_is_active = False
        self._dictation_generation = 0
        self._first_utterance = True  # Suppress turn separator before the very first transcription
        # The listener loop applies each worker result's detected language
        # before processing its transcript, so dispatched queries keep their
        # own locale even while later utterances are being transcribed.
        self._last_detected_language: Optional[str] = None

        # Audio processing components
        self._mlx_model_repo: Optional[str] = None  # For MLX backend
        self.model: Optional[Any] = None  # WhisperModel for faster-whisper, None for MLX
        self.transcribe_lock = threading.Lock()  # Shared lock for Whisper model access
        self._audio_q: queue.Queue = queue.Queue(maxsize=64)
        self._transcription_jobs_q: queue.Queue = queue.Queue(maxsize=8)
        self._transcription_results_q: queue.Queue = queue.Queue()
        self._transcription_worker_thread: Optional[threading.Thread] = None
        self._pre_roll: deque = deque()

        # Audio callback monitoring (for debugging)
        self._callback_count = 0
        self._last_callback_log_time = 0
        self._pending_audio = None
        self._vad_error_logged = False
        self._reset_audio_health()

        # Voice activity detection
        self.is_speech_active = False
        self._silence_frames = 0
        self._utterance_frames: list = []
        self._frame_samples = 0
        self._samplerate = int(getattr(self.cfg, "sample_rate", 16000))
        self._vad: Optional = None

        # Initialise VAD if available
        if webrtcvad is not None and bool(getattr(self.cfg, "vad_enabled", True)):
            try:
                self._vad = webrtcvad.Vad(int(getattr(self.cfg, "vad_aggressiveness", 2)))
            except Exception:
                self._vad = None

        # Initialise modular components
        self.echo_detector = EchoDetector(
            echo_tolerance=float(getattr(self.cfg, "echo_tolerance", 0.3)),
            energy_spike_threshold=float(getattr(self.cfg, "echo_energy_threshold", 2.0))
        )

        self.state_manager = StateManager(
            hot_window_seconds=float(getattr(self.cfg, "hot_window_seconds", 3.0)),
            echo_tolerance=float(getattr(self.cfg, "echo_tolerance", 0.3)),
            voice_collect_seconds=float(getattr(self.cfg, "voice_collect_seconds", 1.0)),
            max_collect_seconds=float(getattr(self.cfg, "voice_max_collect_seconds", 60.0)),
            wake_wait_seconds=float(getattr(self.cfg, "voice_wake_wait_seconds", 4.5)),
        )

        # Energy tracking for echo detection
        self._recent_audio_energy: deque = deque(maxlen=50)

        # Audio-level wake word detection timestamp
        self._wake_timestamp: Optional[float] = None
        # Whether the request being collected or dispatched was addressed with the wake word (or the orb)
        # rather than engaged only by the hot window; short fast routes need it.
        self._request_addressed = True

        # Rolling transcript buffer for context-aware processing
        # Used for both retention and context passed to intent judge
        self._buffer_duration = float(getattr(self.cfg, "transcript_buffer_duration_sec", 120.0))
        self._transcript_buffer = TranscriptBuffer(max_duration_sec=self._buffer_duration)
        debug_log(f"transcript buffer initialised ({self._buffer_duration}s)", "voice")

        # Intent judge (full context, larger model) - always used when available
        self._intent_judge = create_intent_judge(self.cfg)
        if self._intent_judge is not None:
            debug_log(f"intent judge initialised (model: {self._intent_judge.config.model})", "voice")
        else:
            debug_log("intent judge unavailable, using simple wake word detection", "voice")

        self._engaged = False

        # Speaker verification: None unless enabled, enrolled and loadable.
        self._speaker_verifier = create_verifier(self.cfg)
        self._barge_in_reset()

    def stop(self) -> None:
        """Stop the voice listener."""
        self._should_stop = True
        self.state_manager.stop()
        with self._reply_lock:
            for cancelled in self._pending_replies:
                cancelled.set()
            executor, self._reply_executor = self._reply_executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        self._end_engagement()

    @property
    def _dictation_active(self) -> bool:
        return self._dictation_is_active

    @_dictation_active.setter
    def _dictation_active(self, active: bool) -> None:
        if active and not self._dictation_is_active:
            self._dictation_generation += 1
        self._dictation_is_active = active

    def _start_engagement(self) -> None:
        """Mark that Jarvis has been engaged and is working on the user's speech."""
        with self._engagement_lock:
            if self.tts is None or not self.tts.is_speaking():
                self._engaged = True

    def _end_engagement(self) -> None:
        """End the engagement and revert face state to IDLE."""
        with self._engagement_lock:
            if not self._engaged:
                return
            self._engaged = False
        set_state(AssistantState.IDLE)

    def _is_engaged(self) -> bool:
        """Check whether an engagement is in progress."""
        return self._engaged

    def _set_face_state_listening(self) -> None:
        """Set the assistant state to LISTENING."""
        set_state(AssistantState.LISTENING)

    def track_tts_start(self, tts_text: str) -> None:
        """Called when TTS starts speaking."""
        if self.tts and self.tts.enabled:
            # Calculate baseline energy from recent audio samples
            baseline_energy = 0.0045  # default
            if self._recent_audio_energy:
                baseline_energy = sum(self._recent_audio_energy) / len(self._recent_audio_energy)

            self.echo_detector.track_tts_start(tts_text, baseline_energy)
            self._barge_in_settle()

    def activate_hot_window(self) -> None:
        """Activate hot window after TTS completion."""
        debug_log("TTS completed, checking hot window activation", "voice")

        if not self.cfg.hot_window_enabled:
            debug_log("hot window disabled in config, skipping", "voice")
            return

        # Track TTS finish time for echo detection
        self.echo_detector.track_tts_finish()

        # Schedule delayed hot window activation
        debug_log(f"scheduling hot window activation (echo_tolerance={self.state_manager.echo_tolerance}s, hot_window={self.state_manager.hot_window_seconds}s)", "voice")
        self.state_manager.schedule_hot_window_activation(self.cfg.voice_debug)

    def _process_transcript(self, text: str, utterance_energy: float = 0.0, utterance_start_time: float = 0.0, utterance_end_time: float = 0.0, *, captured_during_tts: bool, captured_tts_start_time: float, speaker_rejected: bool = False) -> None:
        """
        Process a transcript from speech recognition.

        Args:
            text: Transcribed text from audio
            utterance_energy: Pre-calculated energy from the utterance frames
            speaker_rejected: The voice was confidently not the owner's; the text
                stays in the transcript buffer as context but cannot engage Jarvis
        """
        if not text or not text.strip():
            self._check_query_timeout()
            return

        text_lower = text.strip().lower()

        if speaker_rejected:
            debug_log("utterance ignored: not the owner's voice", "voice")
            print("  🔇 Heard another voice (ignored)", flush=True)
            self._wake_timestamp = None
            self.state_manager.check_hot_window_expiry(self.cfg.voice_debug)
            return

        # Reset wake timestamp — it must reflect only the current utterance.
        # If this utterance contains a wake word, the early engagement check below
        # will set it. Without this reset, a prior rejected wake-worded
        # utterance would vouch for subsequent unrelated utterances via the
        # `_wake_timestamp is not None` guard in the intent-judge accept path.
        self._wake_timestamp = None

        start_time_str = datetime.fromtimestamp(utterance_start_time).strftime('%H:%M:%S.%f')[:-3] if utterance_start_time > 0 else "N/A"
        end_time_str = datetime.fromtimestamp(utterance_end_time).strftime('%H:%M:%S.%f')[:-3] if utterance_end_time > 0 else "N/A"
        debug_log(f"heard: '{text}' (utterance from {start_time_str} to {end_time_str})", "voice")

        # A queued transcript keeps the TTS context from audio capture.
        received_during_tts = captured_during_tts
        same_tts_context = self.echo_detector._tts_start_time == captured_tts_start_time
        active_tts_overlapped = (
            received_during_tts
            and bool(self.tts and self.tts.is_speaking())
            and same_tts_context
        )

        # --- Early echo check + early engagement ---
        # Check for echo BEFORE starting engagement and BEFORE intent judge.
        # This prevents: false engagement on echo, intent judge blocking the audio
        # loop for seconds on echo, and hot window extending from echo resets.
        if not received_during_tts and not self._is_engaged():
            in_hot_window = self.state_manager.was_speech_during_hot_window(
                utterance_start_time, utterance_end_time
            )
            if in_hot_window:
                # Fuzzy echo check — instant, no intent judge needed.
                # Only catches pure echo (transcript ≈ TTS text). Mixed
                # echo+speech chunks (user spoke over echo) go to the
                # intent judge which can extract the user's speech.
                last_tts_text = self.echo_detector._last_tts_text if same_tts_context else ""
                if last_tts_text:
                    echo_score = fuzz.partial_ratio(
                        text_lower, last_tts_text.lower()
                    )
                    tts_words = len(last_tts_text.split())
                    text_words = len(text_lower.split())
                    is_pure_echo = (
                        echo_score >= 70
                        and text_words <= max(tts_words * 1.3, tts_words + 3)
                    )
                    if is_pure_echo:
                        # Before rejecting, try to salvage user speech appended
                        # after the echo prefix. Whisper commonly merges the tail
                        # of TTS echo with the user's follow-up into a single
                        # transcript; without salvage, the user's real speech
                        # would be dropped before the intent judge ever sees it.
                        # Try exact-word cleanup first (cheapest, most precise),
                        # then fall back to the rightmost-boundary scan which
                        # handles Whisper mis-transcriptions at the echo/speech
                        # join ("explores" → "laws") that exact matching can't.
                        salvaged = self.echo_detector.cleanup_leading_echo(text_lower)
                        if salvaged == text_lower:
                            salvaged_alt = self.echo_detector.salvage_after_echo_tail(text_lower)
                            if salvaged_alt:
                                salvaged = salvaged_alt
                        # Require ≥ min_salvage_words to avoid treating Whisper's
                        # echo-tail hallucinations ("…regions like Steneti") as
                        # genuine user speech. The threshold lives on the echo
                        # detector so every salvage site shares one policy.
                        min_words = self.echo_detector.min_salvage_words
                        if (salvaged != text_lower
                                and len(salvaged.split()) >= min_words):
                            debug_log(
                                f"salvaged user speech from hot-window echo+speech "
                                f"chunk: '{salvaged}'",
                                "voice",
                            )
                            print(
                                f"  ✂️ Stripped echo prefix, kept: \"{salvaged[:60]}"
                                f"{'...' if len(salvaged) > 60 else ''}\"",
                                flush=True,
                            )
                            self._transcript_buffer.update_last_segment_text(salvaged)
                            # text_lower now carries the salvaged query — the rest
                            # of _process_transcript reads from this variable.
                            text_lower = salvaged
                        else:
                            debug_log(f"🔇 Early echo rejection (score={echo_score}): \"{text_lower}\"", "voice")
                            print(f"  🔇 Heard (echo): \"{text_lower[:50]}{'...' if len(text_lower) > 50 else ''}\"", flush=True)
                            return

                # Non-echo (or salvaged) in hot window — start engagement
                self._start_engagement()
                self._set_face_state_listening()
                debug_log("early engagement: hot window active", "voice")
            else:
                # Not in hot window — check for wake word
                wake_word = getattr(self.cfg, "wake_word", "jarvis")
                aliases = list(set(getattr(self.cfg, "wake_aliases", [])) | {wake_word})
                fuzzy_ratio = float(getattr(self.cfg, "wake_fuzzy_ratio", 0.78))
                if is_wake_word_detected(text_lower, wake_word, aliases, fuzzy_ratio):
                    self._wake_timestamp = utterance_start_time
                    self._start_engagement()
                    self._set_face_state_listening()
                    debug_log("early engagement: wake word detected", "voice")

        # Echo rejection & stop commands — only while TTS is actively playing.
        # After TTS finishes, the intent judge handles everything (echo detection,
        # hot window follow-ups, etc.) using full transcript context + last TTS text.
        if self.tts and self.tts.enabled and active_tts_overlapped:
            # Stop command detection (fast, text-based)
            stop_commands = getattr(self.cfg, "stop_commands", ["stop", "quiet", "shush", "silence", "enough", "shut up"])
            if is_stop_command(text_lower, stop_commands):
                debug_log(f"stop command detected during TTS: {text_lower} (energy: {utterance_energy:.4f})", "voice")
                self.tts.interrupt()
                self._barge_in_reset()  # interrupt() also restores a ducked volume
                try:
                    while not self._audio_q.empty():
                        self._audio_q.get_nowait()
                except Exception:
                    pass
                return

            # Echo rejection during active TTS
            should_reject = self.echo_detector.should_reject_as_echo(
                text_lower, utterance_energy, True,
                getattr(self.cfg, 'tts_rate', 200), utterance_start_time
            )
            if should_reject:
                # Try to salvage user speech appended after echo
                salvaged = self.echo_detector.cleanup_leading_echo_during_tts(
                    text_lower,
                    getattr(self.cfg, 'tts_rate', 200),
                    utterance_start_time,
                )
                min_words = self.echo_detector.min_salvage_words
                if (salvaged and salvaged.strip() and salvaged != text_lower
                        and len(salvaged.split()) >= min_words):
                    debug_log(f"salvaged user speech from echo during TTS: '{salvaged}'", "voice")
                    self._transcript_buffer.update_last_segment_text(salvaged)
                    text_lower = salvaged
                else:
                    debug_log(f"echo rejected during TTS: '{text_lower[:50]}'", "echo")
                    print(f"  🔇 Heard (echo): \"{text_lower[:50]}{'...' if len(text_lower) > 50 else ''}\"", flush=True)
                    return

        # Salvage user speech from merged echo+speech chunks.
        # When Whisper delivers a single transcript containing TTS echo followed by
        # user speech (e.g. "I can only provide... Well you can search for it"), the
        # echo portion was captured during TTS but the transcript arrives after TTS
        # finishes. Try to strip the leading echo and use just the user's speech.
        # Skip entirely if there's no prior TTS — nothing to match against.
        last_tts_text_for_salvage = self.echo_detector._last_tts_text if same_tts_context else ""
        last_tts_finish = self.echo_detector._last_tts_finish_time if same_tts_context else 0.0
        # Use echo_tolerance as buffer — speaker/mic latency means the utterance
        # may start slightly after TTS finish yet still contain the echo.
        echo_tol = self.echo_detector.echo_tolerance
        if (last_tts_text_for_salvage and last_tts_finish > 0
                and utterance_start_time > 0
                and utterance_start_time < last_tts_finish + echo_tol):
            salvaged = self.echo_detector._salvage_suffix_from_echo(
                text_lower,
                getattr(self.cfg, 'tts_rate', 200),
                utterance_start_time,
            )
            # If the prefix-based salvage fails or truncates too aggressively
            # (Whisper-mangled echo boundary → exact cleanup misses; fuzzy
            # prefix iteration prefers shortest suffix), fall through to the
            # rightmost-boundary scan which recovers the full follow-up.
            boundary_salvaged = self.echo_detector.salvage_after_echo_tail(text_lower)
            if boundary_salvaged and (
                salvaged is None or salvaged == text_lower
                or len(boundary_salvaged.split()) > len(salvaged.split())
            ):
                salvaged = boundary_salvaged
            min_words = self.echo_detector.min_salvage_words
            if (salvaged and salvaged.strip() and salvaged != text_lower
                    and len(salvaged.split()) >= min_words):
                debug_log(f"salvaged user speech from merged echo+speech chunk: '{salvaged}'", "voice")
                self._transcript_buffer.update_last_segment_text(salvaged)
                text_lower = salvaged

        # Check hot window expiry
        self.state_manager.check_hot_window_expiry(self.cfg.voice_debug)

        # A pending voice confirmation consumes only a reply addressed to Jarvis.
        if self._handle_pending_confirmation(
            text_lower, utterance_start_time, utterance_end_time
        ):
            return

        # Intent judge handles utterances that need conversational interpretation.
        # Gets full transcript context, last TTS text, and hot window state.
        # Handles: echo detection, wake word queries, hot window follow-ups.
        # During active TTS, skip short utterances (<=3 words) as those are
        # handled by stop command detection above.
        is_speaking_now = active_tts_overlapped
        intent_judgment = None

        # Determine if this could be a hot window follow-up.
        # Only use formal hot window state — no time-based grace period.
        # The state manager already handles the timing (echo_tolerance
        # delay before activation, hot_window_seconds before expiry).
        # A generous grace period caused false hot window claims after
        # the user had already seen "Returning to wake word mode".
        could_be_hot_window = self.state_manager.was_speech_during_hot_window(
            utterance_start_time, utterance_end_time
        )

        # While a request is being collected or its reply generated (a voice
        # reply on the reply worker, a background bridge request or a routine,
        # from either voice or chat), a spoken stop addressed with the wake word
        # or in the hot window cancels it here rather than joining or queueing
        # behind it; a routine stops between steps.
        from ..bridge import runtime as _bridge_runtime
        from ..routines import runner as _routines
        _collecting = self.state_manager.is_collecting()
        if self._pending_replies or _collecting or _bridge_runtime.request_active() or _routines.is_running():
            _wake_word = getattr(self.cfg, 'wake_word', 'jarvis')
            _aliases = list(getattr(self.cfg, 'wake_aliases', []))
            _addressed = (
                self._wake_timestamp is not None
                or could_be_hot_window
                or is_wake_word_detected(
                    text_lower, _wake_word, list(set(_aliases) | {_wake_word}),
                    float(getattr(self.cfg, "wake_fuzzy_ratio", 0.78)))
            )
            if _addressed:
                _stop_query = extract_query_after_wake(text_lower, _wake_word, _aliases)
                _stop_commands = getattr(self.cfg, "stop_commands",
                                         ["stop", "quiet", "shush", "silence", "enough", "shut up"])
                if is_stop_command(_stop_query, _stop_commands):
                    debug_log('stop command cancels the reply in flight', 'voice')
                    print("  🛑 Stopped", flush=True)
                    if _collecting:
                        self.state_manager.clear_collection()
                    self._cancel_pending_replies()
                    _bridge_runtime.cancel_active_request("stop")
                    _routines.stop_running()
                    self.state_manager.cancel_hot_window_activation()
                    self._transcript_buffer.mark_segment_processed(text_lower)
                    return

        # A complete routine command reuses normal dispatch, output and query lock.
        # Active/queued TTS and collection fragments need the existing judge path.
        if (not received_during_tts and not is_speaking_now
                and not self.state_manager.is_collecting()
                and (self._wake_timestamp is not None or could_be_hot_window)):
            from ..fastpath.dispatcher import match_command
            wake_word = getattr(self.cfg, 'wake_word', 'jarvis')
            aliases = list(getattr(self.cfg, 'wake_aliases', []))
            fast_query = extract_query_after_wake(text_lower, wake_word, aliases)
            addressed = self._wake_timestamp is not None
            fast_match = match_command(fast_query, self.cfg, self._last_detected_language, addressed=addressed)
            if fast_match is not None:
                self._request_addressed = addressed
                debug_log(f'FAST_ROUTE voice family={fast_match.family} tool={fast_match.tool_name}; judge and collection skipped', 'routing')
                self._mark_turn("fast-command match")
                self.state_manager.cancel_hot_window_activation()
                self._transcript_buffer.mark_segment_processed(text_lower)
                self._dispatch_query(fast_query)
                return

        # A bare wake word carries no request, so there is nothing for the judge to extract.
        # Small judges invent one from the name alone ("Jarvis?" -> "what is Jarvis?"), so
        # wait for the request instead of asking them.
        bare_wake = getattr(self.cfg, "wake_word", "jarvis")
        bare_aliases = list(set(getattr(self.cfg, "wake_aliases", [])) | {bare_wake})
        if is_bare_wake_word(text_lower, bare_wake, bare_aliases):
            debug_log("bare wake word: waiting for the request without the intent judge", "voice")
            self._begin_collection("", text_lower)
            return

        # Use the upgraded intent judge if available (with full transcript context)
        # Allow during TTS for longer utterances (>3 words) that might be user responses
        word_count = len(text_lower.split())
        skip_intent_judge_during_tts = is_speaking_now and word_count <= 3

        # Gate the intent judge on an engagement signal. Without this check the
        # judge was called on every ambient utterance, blocking the audio loop
        # for up to `timeout_sec` on each background chatter — which could
        # cascade into UI freezes when many utterances queued up during a slow
        # or loaded Ollama. The judge adds value only when one of:
        #   1. A wake word was detected in the current utterance
        #   2. We are in (or pending) a hot window following TTS
        #   3. TTS is currently speaking (intent judge can catch responses / stops
        #      that the fast text-based stop command check missed)
        has_engagement_signal = (
            self._wake_timestamp is not None
            or could_be_hot_window
            or is_speaking_now
        )

        if not has_engagement_signal:
            debug_log(
                f"skipping intent judge — no wake word, no hot window, no TTS "
                f"(ambient: \"{text_lower[:40]}{'...' if len(text_lower) > 40 else ''}\")",
                "voice",
            )

        if (
            not skip_intent_judge_during_tts
            and has_engagement_signal
            and self._intent_judge is not None
            and self._intent_judge.available
        ):
            # Get recent transcript segments for context (full buffer)
            context_segments = self._transcript_buffer.get_last_seconds(self._buffer_duration)

            # Get TTS context for echo detection
            last_tts_text = (self.echo_detector._last_tts_text or "") if same_tts_context else ""
            last_tts_finish_time = (self.echo_detector._last_tts_finish_time or 0.0) if same_tts_context else 0.0

            judge_started = time.perf_counter()
            intent_judgment = self._intent_judge.judge(
                segments=context_segments,
                wake_timestamp=self._wake_timestamp,
                last_tts_text=last_tts_text,
                last_tts_finish_time=last_tts_finish_time,
                in_hot_window=could_be_hot_window,
                current_text=text_lower,
            )
            self._mark_turn("intent judge", (time.perf_counter() - judge_started) * 1000)

            if intent_judgment is not None:
                # Log intent judge decision for user visibility
                mode_str = "hot window" if could_be_hot_window else "wake word"
                if intent_judgment.directed:
                    print(f"  🧠 Intent ({mode_str}): directed → \"{intent_judgment.query or text_lower}\"", flush=True)
                else:
                    print(f"  🧠 Intent ({mode_str}): not directed ({intent_judgment.reasoning})", flush=True)
            else:
                reason = self._intent_judge.last_failure_reason or "no segments or unavailable"
                print(f"  🧠 Intent judge: unavailable ({reason})", flush=True)
                debug_log(f"intent judge returned None — falling back ({reason})", "voice")
                # Hot window fallback: if the early echo check already cleared
                # this text, accept it even without the judge's verdict.
                if could_be_hot_window:
                    last_tts_text_fb = (self.echo_detector._last_tts_text or "") if same_tts_context else ""
                    is_pure_echo = False
                    if last_tts_text_fb:
                        echo_score = fuzz.partial_ratio(
                            text_lower, last_tts_text_fb.lower()
                        )
                        tts_words = len(last_tts_text_fb.split())
                        text_words = len(text_lower.split())
                        is_pure_echo = (
                            echo_score >= 70
                            and text_words <= max(tts_words * 1.3, tts_words + 3)
                        )
                    if not is_pure_echo:
                        print(f"  🧠 Intent fallback: accepting hot window speech", flush=True)
                        debug_log(f"✅ Hot window fallback (judge unavailable): \"{text_lower}\"", "voice")
                        self._begin_collection(text_lower, text_lower)
                        return

            if intent_judgment is not None:
                # If judge says stop command, interrupt TTS
                if intent_judgment.stop and active_tts_overlapped:
                    debug_log(f"🛑 Intent judge detected stop command", "voice")
                    self.tts.interrupt()
                    self._barge_in_reset()  # interrupt() also restores a ducked volume
                    return

                # If directed with query, process it
                if intent_judgment.directed and intent_judgment.query:
                    # In wake word mode, verify the wake word is actually present
                    # The LLM sometimes hallucinates wake words that don't exist
                    if not could_be_hot_window:
                        wake_word = getattr(self.cfg, "wake_word", "jarvis")
                        aliases = list(set(getattr(self.cfg, "wake_aliases", [])) | {wake_word})
                        has_wake_word = self._wake_timestamp is not None or is_wake_word_detected(
                            text_lower, wake_word, aliases
                        )
                        if not has_wake_word:
                            print(f"  🧠 Intent override: no wake word found, ignoring", flush=True)
                            debug_log(
                                f"⚠️ Intent judge said directed but no wake word found in '{text_lower[:50]}...' "
                                f"(reasoning: {intent_judgment.reasoning})",
                                "voice"
                            )
                            # Don't accept - fall through to wake word check
                        else:
                            debug_log(f"✅ Intent judge accepted ({intent_judgment.confidence}): \"{intent_judgment.query}\"", "voice")
                            self._begin_collection(intent_judgment.query, text_lower)
                            return
                    else:
                        # Hot window mode - no wake word needed, but check for echo.
                        # The mic can pick up Jarvis's own TTS output and Whisper
                        # transcribes it as user speech. Check fuzzy similarity.
                        # Only reject PURE echo — if the heard text is significantly
                        # longer than TTS, it contains user speech mixed with echo
                        # and the intent judge's extraction should be used instead.
                        if last_tts_text:
                            echo_score = fuzz.partial_ratio(
                                text_lower, last_tts_text.lower()
                            )
                            tts_words = len(last_tts_text.split())
                            text_words = len(text_lower.split())
                            is_pure_echo = (
                                echo_score >= 70
                                and text_words <= max(tts_words * 1.3, tts_words + 3)
                            )
                            if is_pure_echo:
                                # Also check judge's extracted query — if it matches
                                # TTS too, it's genuinely pure echo. If the query is
                                # different, the judge extracted real user speech.
                                query_echo_score = fuzz.partial_ratio(
                                    intent_judgment.query.lower(),
                                    last_tts_text.lower()
                                )
                                if query_echo_score >= 70:
                                    debug_log(f"🔇 Echo in hot window (directed, score={echo_score}): \"{text_lower}\"", "voice")
                                    print(f"  🔇 Heard (echo): \"{text_lower[:50]}{'...' if len(text_lower) > 50 else ''}\"", flush=True)
                                    self._end_engagement()
                                    return
                                else:
                                    debug_log(
                                        f"echo in text (score={echo_score}) but judge extracted "
                                        f"non-echo query: \"{intent_judgment.query}\"", "voice"
                                    )

                        # The intent judge is explicitly designed to prune echo
                        # and extract the actual user query — always prefer its
                        # output when present. Falling back to raw heard text
                        # leaks partially-salvaged echo fragments into tool
                        # calls (e.g. "…amount now? okay, what is his best
                        # song?" reaching webSearch verbatim). If the judge
                        # returns an empty query (rare), fall back to raw text.
                        judge_query = (intent_judgment.query or "").strip()
                        hot_query = judge_query or text_lower
                        if judge_query and judge_query.lower() != text_lower:
                            debug_log(
                                f"using judge query over heard text: "
                                f"\"{judge_query}\" (heard: \"{text_lower[:80]}\")",
                                "voice",
                            )
                        debug_log(f"✅ Intent judge accepted ({intent_judgment.confidence}): \"{hot_query}\"", "voice")
                        self._begin_collection(hot_query, text_lower)
                        return

                # If directed with high confidence but no extracted query, use actual text
                # Per spec: "Hot window input should reflect what the user actually said"
                # This handles cases where intent judge correctly identifies directed speech
                # but fails to extract/synthesize a query (e.g., conversational follow-ups)
                if intent_judgment.directed and intent_judgment.confidence == "high":
                    # In wake word mode, verify the wake word is actually present
                    if not could_be_hot_window:
                        wake_word = getattr(self.cfg, "wake_word", "jarvis")
                        aliases = list(set(getattr(self.cfg, "wake_aliases", [])) | {wake_word})
                        has_wake_word = self._wake_timestamp is not None or is_wake_word_detected(
                            text_lower, wake_word, aliases
                        )
                        if not has_wake_word:
                            print(f"  🧠 Intent override: no wake word found, ignoring", flush=True)
                            debug_log(
                                f"⚠️ Intent judge said directed (no query) but no wake word in '{text_lower[:50]}...'",
                                "voice"
                            )
                            # Fall through to wake word check
                        else:
                            debug_log(f"✅ Intent judge accepted (directed, high confidence, using actual text): \"{text_lower}\"", "voice")
                            self._begin_collection(text_lower, text_lower)
                            return
                    else:
                        # Hot window — echo check before accepting
                        # Only reject pure echo (similar word count to TTS)
                        if last_tts_text:
                            echo_score = fuzz.partial_ratio(
                                text_lower, last_tts_text.lower()
                            )
                            tts_words = len(last_tts_text.split())
                            text_words = len(text_lower.split())
                            is_pure_echo = (
                                echo_score >= 70
                                and text_words <= max(tts_words * 1.3, tts_words + 3)
                            )
                            if is_pure_echo:
                                debug_log(f"🔇 Echo in hot window (directed/no-query, score={echo_score}): \"{text_lower}\"", "voice")
                                print(f"  🔇 Heard (echo): \"{text_lower[:50]}{'...' if len(text_lower) > 50 else ''}\"", flush=True)
                                self._end_engagement()
                                return

                        debug_log(f"✅ Intent judge accepted (directed, high confidence, using actual text): \"{text_lower}\"", "voice")
                        self._begin_collection(text_lower, text_lower)
                        return

                # If not directed with high confidence, check reasoning before rejecting
                if not intent_judgment.directed and intent_judgment.confidence == "high":
                    # Surgical fix: If intent judge claims "echo" but echo system already cleared
                    # this utterance (we reached here, meaning Priority 2 didn't reject), don't
                    # trust the LLM's echo reasoning - fall through to wake word detection instead.
                    # The echo system does actual text similarity matching; the LLM sometimes
                    # hallucinates echo matches that don't exist.
                    reasoning_lower = (intent_judgment.reasoning or "").lower()
                    if "echo" in reasoning_lower:
                        debug_log(
                            f"⚠️ Intent judge claimed echo but echo system cleared - "
                            f"checking if near hot window: \"{text_lower}\"",
                            "voice"
                        )
                        # Check if utterance started shortly after hot window expired
                        # This catches cases where user started speaking just as hot window expired
                        # Use a 2-second grace period after the 3-second hot window
                        hot_window_grace = 2.0
                        last_tts_finish = (self.echo_detector._last_tts_finish_time or 0.0) if same_tts_context else 0.0
                        hot_window_end = last_tts_finish + self.state_manager.hot_window_seconds
                        time_after_hot_window = utterance_start_time - hot_window_end if utterance_start_time > 0 and hot_window_end > 0 else float('inf')

                        if 0 <= time_after_hot_window < hot_window_grace:
                            # Utterance started within grace period after hot window
                            debug_log(
                                f"✅ Accepting as directed: started {time_after_hot_window:.2f}s after hot window expired",
                                "voice"
                            )
                            self._begin_collection(text_lower, text_lower)
                            return

                        # Check could_be_hot_window (handles overlap: utterance
                        # started during TTS but extended into hot window span).
                        # The grace period above only checks utterance_start_time
                        # which is negative for overlapping utterances.
                        if could_be_hot_window:
                            # Verify it's not pure echo before overriding
                            echo_score = 0
                            is_pure_echo = False
                            if last_tts_text:
                                echo_score = fuzz.partial_ratio(
                                    text_lower, last_tts_text.lower()
                                )
                                tts_words = len(last_tts_text.split())
                                text_words = len(text_lower.split())
                                is_pure_echo = (
                                    echo_score >= 70
                                    and text_words <= max(tts_words * 1.3, tts_words + 3)
                                )
                            if is_pure_echo:
                                debug_log(f"🔇 Echo in hot window (echo reasoning confirmed, score={echo_score}): \"{text_lower}\"", "voice")
                                self._end_engagement()
                                return
                            # Mixed echo+speech — override the echo reasoning
                            print(f"  🧠 Intent override: accepting hot window speech (mixed echo+speech)", flush=True)
                            debug_log(
                                f"⚡ Overriding echo reasoning in hot window "
                                f"(echo_score={echo_score}, text longer than TTS): "
                                f"\"{text_lower}\"",
                                "voice"
                            )
                            self._begin_collection(text_lower, text_lower)
                            return

                        # Otherwise fall through to wake word detection
                        debug_log(f"⏭️ Not near hot window ({time_after_hot_window:.2f}s after), falling through to wake word check", "voice")
                        # Continue to wake word detection below
                    else:
                        # Check if text is pure echo of TTS output
                        echo_score = 0
                        is_pure_echo = False
                        if last_tts_text:
                            echo_score = fuzz.partial_ratio(
                                text_lower, last_tts_text.lower()
                            )
                            tts_words = len(last_tts_text.split())
                            text_words = len(text_lower.split())
                            is_pure_echo = (
                                echo_score >= 70
                                and text_words <= max(tts_words * 1.3, tts_words + 3)
                            )

                        if could_be_hot_window and is_pure_echo:
                            # Confirmed pure echo — early check should have caught
                            # this, but handle as safety net.
                            debug_log(f"🔇 Echo in hot window (score={echo_score}): \"{text_lower}\"", "voice")
                            self._end_engagement()
                            return

                        if could_be_hot_window:
                            # Hot window + non-echo speech → user is talking to us.
                            # Override the intent judge rejection — small models
                            # sometimes reject valid follow-ups like "don't you
                            # already know that?" as not directed.
                            print(f"  🧠 Intent override: accepting hot window speech", flush=True)
                            debug_log(
                                f"⚡ Overriding intent judge in hot window "
                                f"(echo_score={echo_score}, reasoning={intent_judgment.reasoning}): "
                                f"\"{text_lower}\"",
                                "voice"
                            )
                            self._begin_collection(text_lower, text_lower)
                            return

                        # Outside hot window — check if wake word is actually present
                        # before trusting the rejection. Small models sometimes
                        # classify wake-worded statements ("the light is bright,
                        # Jarvis") as "not directed" despite the prompt instructing
                        # otherwise. When the wake word is present, fall through to
                        # Priority 4 wake word detection as a safety net.
                        ww_wake = getattr(self.cfg, "wake_word", "jarvis")
                        ww_aliases = set(getattr(self.cfg, "wake_aliases", [])) | {ww_wake}
                        has_real_wake = is_wake_word_detected(text_lower, ww_wake, list(ww_aliases))
                        if has_real_wake:
                            debug_log(
                                f"⚠️ Intent judge rejected wake-worded utterance "
                                f"(reasoning: {intent_judgment.reasoning}) — "
                                f"falling through to wake word detection",
                                "voice"
                            )
                            # Fall through to Priority 4: wake word detection
                        else:
                            debug_log(f"🚫 Intent judge rejected (not directed, high confidence): \"{text_lower}\"", "voice")
                            self._end_engagement()
                            return
                else:
                    # For inconclusive results, fall through to wake word detection
                    debug_log(f"⏭️ Intent judge inconclusive ({intent_judgment.confidence}), checking wake word", "voice")

        # Priority 4: Wake word detection (fallback when intent judge unavailable/inconclusive)
        wake_word = getattr(self.cfg, "wake_word", "jarvis")
        aliases = set(getattr(self.cfg, "wake_aliases", [])) | {wake_word}
        fuzzy_ratio = float(getattr(self.cfg, "wake_fuzzy_ratio", 0.78))

        wake_detected = is_wake_word_detected(text_lower, wake_word, list(aliases), fuzzy_ratio)
        debug_log(f"wake word check: '{wake_word}' in '{text_lower}' → {wake_detected}", "voice")

        if wake_detected:
            # Cancel any pending hot window activation when new query starts
            query_fragment = extract_query_after_wake(text_lower, wake_word, list(aliases))
            self._begin_collection(query_fragment, text_lower)
            return

        # Priority 5: Collection mode handling
        if self.state_manager.is_collecting():
            speech_end = self._turn.speech_end if self._turn is not None else None
            self.state_manager.add_to_collection(text_lower, last_voice_time=speech_end)
            return

        # Priority 6: Non-wake input (ignore)
        # Provide clear debug info about why input was ignored
        intent_info = ""
        if intent_judgment is not None:
            intent_info = f", intent={intent_judgment.directed}/{intent_judgment.confidence}"

        # End any early-started engagement since we're not processing this input
        self._end_engagement()

        if received_during_tts:
            # User spoke during TTS but it wasn't a stop command - this is likely a response
            # to a TTS question that arrived before hot window activated
            debug_log(f"input ignored (during TTS, not a stop command{intent_info}): {text_lower}", "voice")
            try:
                print(f"  ⏳ Heard during TTS (waiting for hot window): \"{text_lower[:50]}{'...' if len(text_lower) > 50 else ''}\"", flush=True)
            except Exception:
                pass
        else:
            debug_log(f"input ignored (no wake word{intent_info}): {text_lower}", "voice")

    def _handle_pending_confirmation(
        self, text_lower: str, utterance_start_time: float, utterance_end_time: float
    ) -> bool:
        """Answer a pending destructive-action confirmation from speech.

        Only a wake-word utterance or speech inside the hot window (which opens
        when Jarvis finishes asking) can answer; background speech neither
        authorises nor cancels. Returns True when the utterance was consumed.
        """
        from ..tools.confirmation import get_confirmation_store, VoiceResponseStatus
        store = get_confirmation_store()
        if not store.has_pending_voice():
            return False

        wake_word = getattr(self.cfg, "wake_word", "jarvis")
        aliases = list(getattr(self.cfg, "wake_aliases", []))
        fuzzy_ratio = float(getattr(self.cfg, "wake_fuzzy_ratio", 0.78))
        addressed = self._wake_timestamp is not None or is_wake_word_detected(
            text_lower, wake_word, list(set(aliases) | {wake_word}), fuzzy_ratio
        )
        in_hot_window = self.state_manager.was_speech_during_hot_window(
            utterance_start_time, utterance_end_time
        )
        if not (addressed or in_hot_window):
            debug_log("Pending confirmation ignored background speech (no wake word or hot window)", "safety")
            return False

        answer = extract_query_after_wake(text_lower, wake_word, aliases) if addressed else text_lower
        if not answer.strip():
            return False

        detected_lang = getattr(self, "_detected_language", "en") or "en"
        voice_status = store.handle_voice_response(answer, language=detected_lang)
        if voice_status == VoiceResponseStatus.AFFIRMATIVE:
            debug_log("Voice confirmation approved by user", "safety")
            # The approved tool runs on a worker thread; the result arrives
            # through _deliver_confirmed_result. The listener returns at once.
            if not store.execute_pending_async(db=self.db, cfg=self.cfg):
                debug_log("Voice confirmation could not be claimed (expired or already taken)", "safety")
                self._deliver_confirmed_result("That confirmation is no longer valid.", False)
            return True
        if voice_status == VoiceResponseStatus.NEGATIVE:
            debug_log("Voice confirmation rejected by user", "safety")
            store.clear_pending()
            self._deliver_confirmed_result("Action cancelled.", False)
            return True
        # Unrelated or expired: the store has dropped the request; the speech
        # continues through normal processing.
        debug_log(f"Voice confirmation {voice_status.value.lower()}: '{answer}'", "safety")
        store.clear_pending()
        return False

    def _deliver_confirmed_result(self, reply: str, success: bool) -> None:
        """Report a confirmed action's outcome through the normal output path.

        Runs on the confirmed-action worker thread (or the listener for
        cancellations); TTS and console output are queue-based and thread-safe.
        """
        print(f"  {'✅' if success else '❌'} {reply}", flush=True)
        self._speak_reply(reply)

    def _dispatch_query(self, query: str) -> None:
        """
        Hand a complete query to the serial reply worker.

        Returns at once, so the listener keeps capturing and transcribing
        while the reply is generated and an engaged stop can cancel it.

        Args:
            query: Complete user query to process
        """
        debug_log(f"dispatching query: '{query}'", "voice")
        turn = self._turn
        self._mark_turn("dispatch")

        set_state(AssistantState.THINKING)

        cancelled = threading.Event()
        with self._reply_lock:
            self._pending_replies = self._pending_replies + (cancelled,)
            if self._reply_executor is None:
                self._reply_executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="jarvis-reply")
            executor = self._reply_executor
        executor.submit(self._generate_reply, query, self._last_detected_language, turn, cancelled,
                        self._request_addressed)

    def _generate_reply(self, query: str, language: Optional[str], turn: Optional[TurnLatency],
                        cancelled: threading.Event, addressed: bool = True) -> None:
        """Run the reply engine for one query on the reply worker and speak it."""
        try:
            if cancelled.is_set():
                return
            from ..reply.engine import run_reply_engine
            from ..daemon import query_lock

            # The shared voice+text query lock keeps voice and text chat from
            # running the engine concurrently against the same dialogue memory.
            # Voice waits for a text query rather than being dropped.
            try:
                with query_lock():
                    if cancelled.is_set():
                        return
                    engine_started = time.perf_counter()
                    reply = run_reply_engine(
                        self.db, self.cfg, None, query, self.dialogue_memory,
                        language=language, addressed=addressed,
                    )
                    if turn is not None:
                        turn.mark("reply engine", (time.perf_counter() - engine_started) * 1000)
            except Exception as e:
                if cancelled.is_set():
                    return
                # Log the error visibly - this should never happen silently
                print(f"\n  ❌ Reply engine error: {e}", flush=True)
                debug_log(f"reply engine exception: {e}", "voice")
                self._end_engagement()
                # Provide user feedback via TTS
                if self.tts and self.tts.enabled:
                    self.tts.speak("Sorry, I encountered an error processing your request.")
                return

            if cancelled.is_set():
                debug_log("reply discarded: stopped while it was being generated", "voice")
                return
            self._speak_reply(reply, turn)
        finally:
            with self._reply_lock:
                self._pending_replies = tuple(
                    flag for flag in self._pending_replies if flag is not cancelled)

    def _cancel_pending_replies(self) -> None:
        """Drop every reply that is queued or being generated."""
        with self._reply_lock:
            for cancelled in self._pending_replies:
                cancelled.set()
        self._end_engagement()

    def _begin_collection(self, query: str, text_lower: str, addressed: Optional[bool] = None) -> None:
        """Accept an engaged utterance and collect the request it starts.

        The collection pause is measured from when the user stopped speaking,
        so Whisper and judge time count towards it. Speech the user has
        already started is kept so it can join the request; only audio
        captured while TTS is playing is discarded as likely echo.
        ``addressed`` defaults to whether this utterance carried the wake word.
        """
        self._request_addressed = self._wake_timestamp is not None if addressed is None else addressed
        self.state_manager.cancel_hot_window_activation()
        self._transcript_buffer.mark_segment_processed(text_lower)
        if self.tts is not None and self.tts.is_speaking():
            self._clear_audio_buffers()
        speech_end = self._turn.speech_end if self._turn is not None else None
        self.state_manager.start_collection(query, last_voice_time=speech_end)
        self._mark_turn("collection started")
        self._start_engagement()
        try:
            print(f"\n✨ Working on it: {self.state_manager.get_pending_query()}")
        except Exception:
            pass

    def _speech_pending(self) -> bool:
        """True while the user is talking or an utterance awaits its transcript."""
        return (
            self.is_speech_active
            or self._transcription_jobs_q.unfinished_tasks > 0
            or not self._transcription_results_q.empty()
        )

    def _mark_turn(self, stage: str, duration_ms: Optional[float] = None) -> None:
        if self._turn is not None:
            self._turn.mark(stage, duration_ms)

    def _speak_reply(self, reply: Optional[str], turn: Optional[TurnLatency] = None) -> None:
        """Speak a reply and open the hot window when it finishes."""
        if reply and self.tts and self.tts.enabled:
            # End the engagement when TTS starts
            self._end_engagement()

            # TTS completion callback for hot window
            def _on_tts_complete():
                import time as _time
                debug_log(f"TTS completion callback triggered at {_time.time():.3f}", "voice")
                self.activate_hot_window()

            # Duration callback to update echo detector with exact timing (Piper only)
            def _on_duration_known(duration: float):
                debug_log(f"TTS exact duration: {duration:.2f}s", "voice")
                if self.echo_detector:
                    self.echo_detector._tts_exact_duration = duration

            def _on_first_audio():
                if turn is not None:
                    turn.mark("first TTS audio")

            # Track TTS start for echo detection with actual text
            self.track_tts_start(reply)
            debug_log(f"starting TTS for reply ({len(reply)} chars)", "voice")
            if turn is not None:
                turn.mark("TTS requested")

            self.tts.speak(reply, completion_callback=_on_tts_complete,
                          duration_callback=_on_duration_known,
                          first_audio_callback=_on_first_audio)
        else:
            debug_log(f"no TTS output: reply={bool(reply)}, tts={bool(self.tts)}, enabled={getattr(self.tts, 'enabled', False) if self.tts else False}", "voice")
            # End the engagement if there is no TTS response
            self._end_engagement()

    def _calculate_audio_energy(self, frames: list) -> float:
        """Calculate RMS energy from audio frames."""
        if not frames or np is None:
            return 0.0
        try:
            audio_data = np.concatenate(frames)
            rms = float(np.sqrt(np.mean(np.square(audio_data))))
            return rms
        except Exception:
            return 0.0

    def _clear_audio_buffers(self) -> None:
        """Clear all audio buffers and reset speech state.

        Call this on state transitions to prevent old audio from being
        incorrectly concatenated with new input.
        """
        self._utterance_frames = []
        self._pre_roll.clear()
        self._pending_audio = None
        self.is_speech_active = False
        self._silence_frames = 0

        # Clear wake detection state
        self._wake_timestamp = None

        # Drain the audio queue
        try:
            while not self._audio_q.empty():
                self._audio_q.get_nowait()
        except Exception:
            pass

        debug_log("audio buffers cleared", "voice")

    def _is_speech_frame(self, frame) -> bool:
        """Determine if audio frame contains speech."""
        if np is None:
            return True

        # Track energy for echo detection
        rms = float(np.sqrt(np.mean(np.square(frame))))
        self._recent_audio_energy.append(rms)

        if self._vad is None:
            return rms >= float(getattr(self.cfg, "voice_min_energy", 0.0045))

        # Use WebRTC VAD
        try:
            vad_audio = _resample(frame.flatten(), getattr(self, "_stream_samplerate", self._samplerate), 16000)
            pcm16 = np.clip(vad_audio * 32768.0, -32768, 32767).astype(np.int16).tobytes()
            return bool(self._vad.is_speech(pcm16, 16000))
        except Exception as exc:
            if not self._vad_error_logged:
                self._vad_error_logged = True
                debug_log(f"VAD rejected audio frame: {exc}", "voice")
                print("  ⚠️  Speech detection failed; using audio-level detection. Enable voice_debug for details.", flush=True)
            return rms >= float(getattr(self.cfg, "voice_min_energy", 0.0045))

    def _emit_low_confidence(self, event: LowConfidenceEvent) -> None:
        """Notify a consumer without allowing its failure to interrupt listening."""
        if self.on_low_confidence is None:
            return
        try:
            self.on_low_confidence(event)
        except Exception as exc:
            debug_log(f"low-confidence callback failed ({type(exc).__name__})", "voice")

    def _filter_noisy_segments(self, segments):
        """Filter out low-confidence Whisper segments."""
        min_confidence = getattr(self.cfg, "whisper_min_confidence", 0.3)
        marginal_threshold = min_confidence / 3  # Show user-visible log for marginal confidence
        # Threshold above which a segment is considered non-speech (hallucination during silence).
        # Checked independently of avg_logprob because Whisper can be confident about a
        # hallucinated phrase even when no real speech is present.
        no_speech_threshold = getattr(self.cfg, "whisper_no_speech_threshold", 0.5)
        filtered = []
        low_confidence_events = []

        for seg in segments:
            # Hard filter: high no_speech_prob means no real speech regardless of logprob.
            if hasattr(seg, 'no_speech_prob') and is_whisper_hallucination(seg.no_speech_prob, no_speech_threshold):
                debug_log(
                    f"segment filtered (no_speech_prob={seg.no_speech_prob:.2f}): '{seg.text[:50]}'",
                    "voice",
                )
                continue

            confidence = None
            if hasattr(seg, 'avg_logprob'):
                confidence = min(1.0, max(0.0, (seg.avg_logprob + 1.0)))
            elif hasattr(seg, 'no_speech_prob'):
                confidence = 1.0 - seg.no_speech_prob

            if confidence is not None and confidence < min_confidence:
                if self.on_low_confidence is not None:
                    low_confidence_events.append(LowConfidenceEvent(confidence, seg.text))
                if confidence >= marginal_threshold:
                    # Marginal confidence - show in log viewer (not debug)
                    print(f"🔇 Low confidence ({confidence:.2f}): \"{seg.text.strip()[:50]}...\"", flush=True)
                else:
                    # Very low confidence - debug only
                    debug_log(f"segment filtered (confidence={confidence:.2f}): '{seg.text}'", "voice")
                continue

            filtered.append(seg)

        return filtered, tuple(low_confidence_events)

    def _is_repetitive_hallucination(self, text: str) -> bool:
        """
        Detect repetitive hallucinations that Whisper produces on quiet/ambiguous audio.

        Common patterns include repeated single words like "don't don't don't..."
        or repeated short phrases. Also detects character-level repetition patterns
        like "Jろ Jろ Jろ..." which may appear with or without spaces.

        Args:
            text: Transcribed text to check

        Returns:
            True if the text appears to be a hallucination
        """
        import re
        from collections import Counter

        if not text:
            return False

        text_stripped = text.strip()
        if len(text_stripped) < 6:
            return False

        # --- Character-level repetition detection ---
        # Remove all whitespace to detect patterns like "Jろ Jろ Jろ" or "JろJろJろ"
        text_no_space = re.sub(r'\s+', '', text_stripped.lower())

        # Look for repeating patterns of 1-5 characters appearing 3+ times consecutively
        # This catches "JろJろJろJろ" (pattern "Jろ" repeating)
        for pattern_len in range(1, 6):
            if len(text_no_space) < pattern_len * 3:
                continue

            # Check if text is mostly composed of a repeating pattern
            for start in range(pattern_len):
                pattern = text_no_space[start:start + pattern_len]
                if not pattern:
                    continue

                # Count how many times this pattern repeats consecutively from this start position
                remaining = text_no_space[start:]
                repeat_count = 0
                pos = 0
                while pos + pattern_len <= len(remaining) and remaining[pos:pos + pattern_len] == pattern:
                    repeat_count += 1
                    pos += pattern_len

                # If pattern repeats 4+ times and covers most of the string, it's a hallucination
                covered_chars = repeat_count * pattern_len
                coverage = covered_chars / len(text_no_space) if text_no_space else 0

                if repeat_count >= 4 and coverage >= 0.6:
                    debug_log(f"char-level repetition detected: pattern '{pattern}' repeats {repeat_count}x, coverage={coverage:.0%}", "voice")
                    return True

        # --- Word-level repetition detection (existing logic) ---
        words = text_stripped.lower().split()
        if len(words) < 4:
            return False

        # Strip punctuation from words for comparison (handles "word..." vs "word")
        clean_words = [re.sub(r'[^\w]', '', w) for w in words]
        clean_words = [w for w in clean_words if w]  # Remove empty strings

        if len(clean_words) < 4:
            return False

        word_counts = Counter(clean_words)
        most_common_word, most_common_count = word_counts.most_common(1)[0]

        # If a single word makes up more than 50% of all words and appears 4+ times
        if most_common_count >= 4 and most_common_count / len(clean_words) > 0.5:
            debug_log(f"repetitive hallucination detected: '{most_common_word}' repeated {most_common_count}x in '{text[:50]}...'", "voice")
            return True

        # Check for repeated consecutive sequences (e.g., "don don don" or "stop stop stop")
        # Look for any word repeated 3+ times consecutively
        consecutive_count = 1
        for i in range(1, len(clean_words)):
            if clean_words[i] == clean_words[i-1]:
                consecutive_count += 1
                if consecutive_count >= 3:
                    debug_log(f"consecutive repetition detected: '{clean_words[i]}' repeated {consecutive_count}+ times", "voice")
                    return True
            else:
                consecutive_count = 1

        return False

    # Set from any thread (orb click); consumed on the listener thread.
    _manual_wake_toggle = False

    def toggle_manual_wake(self) -> None:
        """Stop a reply being generated or spoken; otherwise wake as if the wake word were
        spoken alone, or deactivate if already woken (thread-safe)."""
        if self._stop_reply_in_progress():
            return
        debug_log("manual wake toggle requested", "voice")
        self._manual_wake_toggle = True

    def _stop_reply_in_progress(self) -> bool:
        """Stop whatever reply is being generated or spoken at once. Returns whether there was one."""
        from ..bridge import runtime as bridge_runtime
        from ..routines import runner as routines
        speaking = self.tts is not None and self.tts.is_speaking()
        bridge_active = bridge_runtime.request_active()
        routine_active = routines.is_running()
        if not (self._pending_replies or speaking or bridge_active or routine_active):
            return False
        debug_log("manual wake: stopping the reply in progress", "voice")
        print("  🛑 Stopped", flush=True)
        self._cancel_pending_replies()
        if bridge_active:
            bridge_runtime.cancel_active_request("stop")
        if routine_active:
            routines.stop_running()
        if speaking:
            self.tts.interrupt()
            self._barge_in_reset()  # interrupt() also restores a ducked volume
        self.state_manager.cancel_hot_window_activation()
        set_state(AssistantState.IDLE)
        return True

    def _apply_manual_wake_toggle(self) -> None:
        """Wake if idle; drop back to idle if waiting for a request or in the hot window."""
        if self._dictation_active:
            return
        if self.state_manager.is_collecting():
            debug_log("manual wake: deactivating, discarding the pending request", "voice")
            self.state_manager.clear_collection()
            self._end_engagement()
        elif self.state_manager.is_hot_window_active():
            debug_log("manual wake: deactivating, ending the hot window", "voice")
            self.state_manager.expire_hot_window(self.cfg.voice_debug)
            self._end_engagement()
        else:
            debug_log("manual wake: waiting for the request", "voice")
            self._begin_collection("", "", addressed=True)

    def _check_query_timeout(self) -> None:
        """Check if there's a pending query that has timed out, and check hot window expiry."""
        if self._manual_wake_toggle:
            self._manual_wake_toggle = False
            self._apply_manual_wake_toggle()
        if self.state_manager.is_collecting():
            self.state_manager.note_voice_activity(self._last_voice_frame_time)
        if self.state_manager.check_collection_timeout(speech_pending=self._speech_pending()):
            query = self.state_manager.clear_collection()
            if query.strip():
                self._dispatch_query(query)

        # Also check hot window expiry - this ensures the timeout is enforced
        # even when there's no audio being processed
        self.state_manager.check_hot_window_expiry(self.cfg.voice_debug)

    def _reset_audio_health(self, now=None):
        now = time.monotonic() if now is None else now
        self._audio_started = now
        self._last_audio_callback = now
        self._last_health_check = now
        self._audio_peak = 0.0
        self._audio_frames_seen = 0
        self._speech_frames_seen = 0
        self._audio_dropped = 0
        self._callback_status = ''
        self._audio_callback_error = ''
        self._audio_health_warning = None

    def _check_audio_health(self, now=None):
        """Report capture failures outside the real-time callback, without recording audio."""
        now = time.monotonic() if now is None else now
        if self._dictation_active:
            self._reset_audio_health(now)
            return
        if now - self._last_health_check < 5:
            return
        self._last_health_check = now
        warning = None
        if now - self._last_audio_callback >= 5:
            warning = 'No microphone callbacks in the last 5 seconds'
        elif self._audio_peak <= 1e-7 and now - self._audio_started >= 10:
            warning = 'Microphone is delivering silent samples'
        if warning and warning != self._audio_health_warning:
            print(f"  ⚠️  {warning}. Check the selected input, mute and recording permissions.", flush=True)
            if sys.platform.startswith('linux'):
                print("     🎤 Check PipeWire/PulseAudio recording-source routing (pavucontrol or wpctl status); avoid monitor/output sources.", flush=True)
        elif not warning and self._audio_health_warning:
            print("  ✅ Microphone audio is arriving again.", flush=True)
        self._audio_health_warning = warning
        if self._callback_status or self._audio_callback_error or self._audio_dropped:
            print(f"  ⚠️  Audio capture: {self._callback_status or self._audio_callback_error or 'queue full'}; {self._audio_dropped} blocks dropped.", flush=True)
            self._callback_status = self._audio_callback_error = ''
            self._audio_dropped = 0
        if self.cfg.voice_debug:
            debug_log(
                f"Audio capture: callbacks={self._callback_count}, frames={self._audio_frames_seen}, "
                f"speech_frames={self._speech_frames_seen}, peak={self._audio_peak:.6f}, "
                f"rate={self._stream_samplerate} Hz", "voice",
            )
        self._audio_peak = 0.0

    def _start_transcription_worker(self) -> None:
        if self._should_stop or self._transcription_worker_thread is not None:
            return
        self._transcription_worker_thread = threading.Thread(
            target=self._run_transcription_worker,
            daemon=True,
            name="jarvis-whisper-transcription",
        )
        self._transcription_worker_thread.start()
        debug_log("started serial Whisper transcription worker", "voice")

    def _transcription_is_current(self, generation: int) -> bool:
        return (
            not self._should_stop
            and not self._dictation_active
            and generation == self._dictation_generation
        )

    def _run_transcription_worker(self) -> None:
        while True:
            job = self._transcription_jobs_q.get()
            if job is None:
                return
            try:
                self._transcribe_job(job)
            finally:
                # A job stays pending until its result is queued for the listener.
                self._transcription_jobs_q.task_done()

    def _transcribe_job(self, job: _TranscriptionJob) -> None:
        if not self._transcription_is_current(job.dictation_generation):
            return
        transcribe_started = time.time()
        try:
            text, language, low_confidence_events = self._transcribe_audio(job.audio)
        except Exception as exc:
            debug_log(f"transcription worker error: {exc}", "voice")
            text, language, low_confidence_events = "", None, ()
        speaker_verdict = self._verify_speaker(job)
        TurnLatency(job.speech_end_time).mark(
            f"Whisper (queued {(transcribe_started - job.end_time) * 1000:.0f} ms, "
            f"{self._whisper_device or self._whisper_backend}/{self._whisper_compute or 'default'})",
            (time.time() - transcribe_started) * 1000,
        )
        if not self._transcription_is_current(job.dictation_generation):
            return
        self._transcription_results_q.put(
            _TranscriptionResult(
                text=text,
                language=language,
                low_confidence_events=low_confidence_events,
                start_time=job.start_time,
                end_time=job.end_time,
                speech_end_time=job.speech_end_time,
                energy=job.energy,
                dictation_generation=job.dictation_generation,
                captured_during_tts=job.captured_during_tts,
                captured_tts_start_time=job.captured_tts_start_time,
                speaker_verdict=speaker_verdict,
            )
        )

    def _verify_speaker(self, job: _TranscriptionJob) -> Verdict:
        """Judge whether a captured utterance is the owner's voice, on the worker thread.

        Speech captured while TTS plays is never judged here: Jarvis's own voice
        is in it, and echo handling and barge-in own that case.
        """
        verifier = self._speaker_verifier
        if verifier is None or job.captured_during_tts:
            return Verdict.UNKNOWN
        try:
            verdict = verifier.check(job.audio)
        except Exception as exc:
            # Verification never blocks speech, and an error here must not end the Whisper worker.
            debug_log(f"speaker check failed ({type(exc).__name__}); not blocking speech", "voice")
            return Verdict.UNKNOWN
        score = getattr(verifier, "last_score", None)
        debug_log(f"speaker check: {verdict.value}"
                  + (f" (score {score:.2f})" if score is not None else ""), "voice")
        return verdict

    def _finish_transcription_worker(self) -> None:
        worker = self._transcription_worker_thread
        if worker is None:
            return
        self._should_stop = True
        while True:
            try:
                self._transcription_jobs_q.get_nowait()
            except queue.Empty:
                break
        self._transcription_jobs_q.put_nowait(None)
        worker.join(timeout=0.5)
        self._transcription_worker_thread = None
        while True:
            try:
                self._transcription_results_q.get_nowait()
            except queue.Empty:
                break
        if worker.is_alive():
            debug_log("Whisper transcription still finishing after listener shutdown", "voice")
        else:
            debug_log("finished queued Whisper transcription work", "voice")

    def _handle_transcription_result(self, result: _TranscriptionResult) -> None:
        try:
            self._apply_transcription_result(result)
        finally:
            self._resolve_barge_in()

    def _barge_in_reset(self) -> None:
        """Forget barge-in progress; called when a reply starts or speech ends."""
        self._barge_in_fired = False
        self._barge_in_deadline = 0.0
        self._barge_in_voiced_frames = 0
        self._barge_in_next_check = 0
        self._barge_in_passes = 0

    def _barge_in_settle(self) -> None:
        """Forget barge-in progress, restoring the volume first if a barge-in had ducked TTS.

        The duck outlives the reply it was made for, so every path that drops the state while a
        barge-in is active must unduck, or later replies start at the ducked volume."""
        if self._barge_in_fired:
            self._barge_in_resume()
        self._barge_in_reset()

    def _barge_in_tick(self, is_voice: bool) -> None:
        """Duck TTS as soon as the owner's voice is heard over it, before Whisper runs.

        Jarvis's own voice is a different speaker, so voiced audio during
        playback that verifies as the owner for ``barge_in_verify_ms`` is the
        owner talking over it. The check slides: the most recent
        ``barge_in_verify_ms`` of audio is re-scored every 100 ms of voiced
        speech, so the owner joining a long stretch of Jarvis's own voice is
        found as soon as they have spoken for that long.
        """
        tts = self.tts
        if tts is None or not tts.is_speaking():
            if self._barge_in_fired or self._barge_in_voiced_frames:
                self._barge_in_settle()
            return
        now = time.time()
        if self._barge_in_fired:
            if now >= self._barge_in_deadline:
                debug_log("barge-in unresolved; restoring TTS volume", "voice")
                self._barge_in_resume()
            return
        verifier = self._speaker_verifier
        if (not is_voice or verifier is None
                or not bool(getattr(self.cfg, "barge_in_enabled", True))):
            return
        frame_ms = max(1, self._frame_ms)
        self._barge_in_voiced_frames += 1
        if self._barge_in_voiced_frames < self._barge_in_next_check:
            return
        window = max(1, int(int(getattr(self.cfg, "barge_in_verify_ms", 300)) / frame_ms))
        if self._barge_in_voiced_frames < window:
            return
        self._barge_in_next_check = self._barge_in_voiced_frames + max(1, 100 // frame_ms)
        try:
            audio = np.concatenate(self._utterance_frames[-window:], axis=0).flatten()
            stream_rate = getattr(self, "_stream_samplerate", self._samplerate)
            if stream_rate != self._samplerate:
                audio = _resample(audio, stream_rate, self._samplerate)
            score = verifier.score(audio, min_seconds=0.25)
        except Exception as exc:
            debug_log(f"barge-in check failed ({type(exc).__name__})", "voice")
            return
        if score is None or score < float(getattr(self.cfg, "speaker_barge_in_threshold", 0.2)):
            self._barge_in_passes = 0
            return
        self._barge_in_passes += 1
        if self._barge_in_passes < self._BARGE_IN_CONFIRMATIONS:
            return
        self._barge_in_fired = True
        self._barge_in_deadline = now + 6.0
        debug_log(f"barge-in: owner heard over TTS after {window * frame_ms} ms of speech", "voice")
        duck = getattr(tts, "duck", None)
        (duck if callable(duck) else tts.interrupt)()

    def _barge_in_resume(self) -> None:
        """Restore TTS volume after a barge-in that led nowhere."""
        tts = self.tts
        self._barge_in_fired = False
        self._barge_in_deadline = 0.0
        unduck = getattr(tts, "unduck", None)
        if tts is not None and callable(unduck):
            unduck()

    def _resolve_barge_in(self) -> None:
        """Let the transcript decide a ducked reply: a request or stop ends it, chatter resumes it."""
        if not self._barge_in_fired:
            return
        tts = self.tts
        if tts is None or not tts.is_speaking():
            self._barge_in_settle()
            return
        if self.state_manager.is_collecting() or self._pending_replies:
            debug_log("barge-in: request heard, stopping TTS", "voice")
            self._barge_in_reset()
            tts.interrupt()
        else:
            debug_log("barge-in: no request heard, resuming TTS", "voice")
            self._barge_in_resume()

    def _apply_transcription_result(self, result: _TranscriptionResult) -> None:
        if not self._transcription_is_current(result.dictation_generation):
            return
        for event in result.low_confidence_events:
            if not self._transcription_is_current(result.dictation_generation):
                return
            self._emit_low_confidence(event)
        if not self._transcription_is_current(result.dictation_generation):
            return
        self._last_detected_language = result.language
        self._turn = TurnLatency(result.speech_end_time)
        text = result.text
        if not text or not text.strip():
            self.state_manager.check_hot_window_expiry(self.cfg.voice_debug)
            return

        separator = "" if self._first_utterance else f"\n{'─' * 50}"
        self._first_utterance = False
        print(f"{separator}\n📝 Heard: \"{text}\"", flush=True)

        if self._is_repetitive_hallucination(text):
            debug_log(f"rejected repetitive hallucination: '{text[:80]}...'", "voice")
            self.state_manager.check_hot_window_expiry(self.cfg.voice_debug)
            return

        self._transcript_buffer.add(
            text=text,
            start_time=result.start_time,
            end_time=result.end_time,
            energy=result.energy,
            is_during_tts=result.captured_during_tts,
        )
        self._process_transcript(
            text, result.energy, result.start_time, result.end_time,
            captured_during_tts=result.captured_during_tts,
            captured_tts_start_time=result.captured_tts_start_time,
            speaker_rejected=getattr(result, "speaker_verdict", None) is Verdict.OTHER,
        )

    def _audio_frames(self, buf):
        """Keep native-rate frame boundaries across arbitrary callback block sizes."""
        mono = mono_capture(buf)
        if mono.size:
            self._audio_peak = max(self._audio_peak, float(np.max(np.abs(mono))))
        if self._pending_audio is not None:
            mono = np.concatenate((self._pending_audio, mono))
        count = len(mono) // self._frame_samples
        end = count * self._frame_samples
        self._pending_audio = mono[end:].copy()
        self._audio_frames_seen += count
        return [mono[start:start + self._frame_samples] for start in range(0, end, self._frame_samples)]

    def _on_audio(self, indata, frames, time_info, status):
        """Audio callback from sounddevice."""
        try:
            self._last_audio_callback = time.monotonic()
            self._callback_count += 1
            if status:
                self._callback_status = str(status)
            if self._should_stop or self._dictation_active:
                return
            chunk = (indata.copy() if hasattr(indata, "copy") else indata)
            try:
                self._audio_q.put_nowait(chunk)
            except queue.Full:
                self._audio_dropped += 1
        except Exception as exc:
            self._audio_callback_error = str(exc)

    def _determine_whisper_backend(self) -> str:
        """Determine which Whisper backend to use based on config and availability."""
        backend_pref = getattr(self.cfg, "whisper_backend", "auto")

        if backend_pref == "mlx":
            if MLX_WHISPER_AVAILABLE:
                return "mlx"
            debug_log("MLX Whisper requested but not available, falling back to faster-whisper", "voice")
            return "faster-whisper"

        if backend_pref == "faster-whisper":
            return "faster-whisper"

        # Auto mode: prefer MLX on Apple Silicon
        if MLX_WHISPER_AVAILABLE and _is_apple_silicon():
            return "mlx"

        return "faster-whisper"

    def _apply_whisper_load_success(
        self, model_name: str, try_device: str, try_compute: str,
        device: str, compute: str, cpu_threads: int,
        context: str = "",
    ) -> str:
        """Record state and print diagnostics after a successful Whisper model load.

        Returns the resolved device string.
        """
        ct2_model = getattr(self.model, "model", None)
        resolved_device = str(getattr(ct2_model, "device", try_device)).lower()
        debug_log(
            f"faster-whisper initialised{context}: name={model_name}, "
            f"device={resolved_device}, compute={try_compute}, "
            f"cpu_threads={cpu_threads}",
            "voice",
        )
        self._whisper_device = resolved_device
        self._whisper_compute = try_compute

        if try_device != device and device in ("auto", "cuda"):
            print("     ⚠️  CUDA not available, using CPU (this may be slower)", flush=True)
            print("     💡 Tip: Install NVIDIA CUDA toolkit for faster speech recognition", flush=True)
        if try_compute != compute:
            print(f"     ⚠️  Using '{try_compute}' compute type ('{compute}' not supported)", flush=True)
        if resolved_device == "cpu":
            print(f"     ⚡ CPU mode: using {cpu_threads} threads with optimised decoding", flush=True)

        suffix = f" ({context})" if context else ""
        print(f"     🎤 Whisper '{model_name}' loaded on {resolved_device}{suffix}", flush=True)
        return resolved_device

    def _report_llm_warmup(self) -> None:
        """Describe model probes without claiming full role requests were tested."""
        results = self._llm_warmup_results
        groups = {}
        for key, label in (('chat', 'chat'), ('judge', 'intent judge'), ('router', 'tool router')):
            if key in results:
                groups.setdefault(results[key], []).append(label)
        for (name, ok), roles in groups.items():
            status = 'passed' if ok else 'failed'
            icon = '🔥' if ok else '⚠️'
            print(f"     {icon} Model '{name}': warmup probe {status} (used by {', '.join(roles)})", flush=True)
        if groups:
            print("     ℹ️ Warmup checks model loading, not full requests or their timeouts.", flush=True)
        if 'judge' in results:
            timeout = float(getattr(self.cfg, 'intent_judge_timeout_sec', 6.0))
            print(f"     🧠 Intent detection: {timeout:g}s timeout (full intent request not tested)", flush=True)
        if 'embed' in results:
            name, ok = results['embed']
            if ok:
                print(f"     📐 Embedding probe passed: '{name}'", flush=True)
            else:
                print(f"     ⚠️ Embedding probe failed: '{name}'. Check model availability and embedding settings.", flush=True)

    def _start_llm_warmup(self) -> list[threading.Thread]:
        """Pre-load chat and intent judge models via the active backend.

        Warmup goes through ``warm_up_chat_model`` → ``LLMBackend.warm_up``,
        so it pages models into Ollama's resident memory on the Ollama path
        and sends a minimal inference to load the model on an OpenAI-
        compatible server. Starts daemon threads concurrently so
        warmup overlaps with Whisper initialisation. When both models point
        at the same model, a single warmup covers both.

        Results land in ``self._llm_warmup_results`` keyed by role. The
        caller joins the returned threads with a shared deadline before
        reporting the probe outcomes and announcing "Listening!". Full
        role-specific requests and their deadlines are not exercised.
        """
        self._llm_warmup_results: dict[str, tuple[str, bool]] = {}

        if _is_low_power_mode_enabled(self.cfg):
            print("     🌱 Low power mode: LLM warmup skipped", flush=True)
            debug_log("low power mode enabled: skipping LLM warmup", "voice")
            return []

        chat_model = str(getattr(self.cfg, "llm_chat_model", "") or "").strip()
        # The chat model writes replies only in local reply mode, so a Codex or
        # Claude session never pre-loads it.
        reply_mode = reply_modes.active_mode()
        if chat_model and reply_mode != reply_modes.LOCAL:
            print(f"     ☁️ Chat model '{chat_model}' not pre-loaded ({reply_modes.LABELS[reply_mode]} replies)", flush=True)
            debug_log(f"reply mode {reply_mode}: skipping chat model warmup", "voice")
            chat_model = ""

        chat_timeout = float(getattr(self.cfg, "llm_routing_timeout_sec", 8.0))
        judge = self._intent_judge
        judge_model = judge.config.model if judge is not None else ""
        shared_judge = bool(chat_model) and judge_model == chat_model

        # Tool router — only warmed when the LLM selection strategy is active
        # AND it points at a model distinct from chat/judge. Routing runs on
        # the fast tier; resolving through the same tier helper the reply
        # engine uses keeps warmup targeting whatever the engine will actually
        # call. Skipping warmup for non-LLM strategies avoids loading a model
        # that won't be used this session.
        strategy = str(getattr(self.cfg, "tool_selection_strategy", "") or "").lower()
        from ..llm import resolve_model, Tier
        router_model_effective = resolve_model(self.cfg, Tier.FAST)
        router_model = router_model_effective if strategy == "llm" else ""
        shared_router = bool(router_model) and router_model in {chat_model, judge_model}

        embed_model = str(getattr(self.cfg, "embedding_model", "") or "").strip()

        threads: list[threading.Thread] = []

        if chat_model:
            def _warm_chat() -> None:
                ok = warm_up_chat_model(self.cfg, chat_model, timeout=chat_timeout)
                self._llm_warmup_results["chat"] = (chat_model, ok)
                # When chat and judge share a model, one warmup covers both.
                if shared_judge:
                    self._llm_warmup_results["judge"] = (chat_model, ok)
                # Router reusing chat_model is already covered.
                if router_model and router_model == chat_model:
                    self._llm_warmup_results["router"] = (chat_model, ok)

            threads.append(threading.Thread(target=_warm_chat, daemon=True, name="warmup-chat"))

        if judge is not None and not shared_judge:
            def _warm_judge() -> None:
                ok = judge.warm_up()
                self._llm_warmup_results["judge"] = (judge_model, ok)
                if router_model and router_model == judge_model:
                    self._llm_warmup_results["router"] = (judge_model, ok)

            threads.append(threading.Thread(target=_warm_judge, daemon=True, name="warmup-judge"))

        if router_model and not shared_router:
            def _warm_router() -> None:
                ok = warm_up_chat_model(self.cfg, router_model, timeout=chat_timeout)
                self._llm_warmup_results["router"] = (router_model, ok)

            threads.append(threading.Thread(target=_warm_router, daemon=True, name="warmup-router"))

        # Chat success cannot establish support for the embeddings endpoint,
        # even when both settings happen to name the same model.
        if embed_model:
            def _warm_embed() -> None:
                try:
                    backend = get_embedding_backend(self.cfg)
                    # Use embed() rather than warm_up() because embedding-only
                    # models (e.g. nomic-embed-text, modernbert) are not served
                    # on the chat endpoint — warm_up() sends a chat completion
                    # which would fail for those models. A single-token embedding
                    # request forces the runtime to load the model the same way.
                    # Embedding probes use the backend's own timeout (15s),
                    # independently of inference routing budgets.
                    result = backend.embed("ping", embed_model)
                    ok = result is not None
                except Exception as exc:
                    debug_log(f"embed warmup failed: {exc}", "voice")
                    ok = False
                self._llm_warmup_results["embed"] = (embed_model, ok)

            threads.append(threading.Thread(target=_warm_embed, daemon=True, name="warmup-embed"))

        for t in threads:
            t.start()

        debug_log(
            f"LLM warmup started (chat={chat_model or 'n/a'}, "
            f"judge={judge_model or 'n/a'}, router={router_model or 'n/a'}, "
            f"embed={embed_model or 'n/a'}, "
            f"shared_judge={shared_judge}, shared_router={shared_router})",
            "voice",
        )
        return threads

    def _weather_example(self, wake_title: str) -> str:
        """Return the weather query example for the startup banner.

        Shows the plain form when a location source is configured, or the
        [your city] placeholder form so the user knows to supply a city.
        """
        location_enabled = getattr(self.cfg, "location_enabled", True)
        location_auto_detect = getattr(self.cfg, "location_auto_detect", True)
        location_ip_address = getattr(self.cfg, "location_ip_address", None)
        location_known = (
            location_enabled
            and (location_auto_detect or bool(location_ip_address))
            and is_location_available()
        )
        if location_known:
            return f"\"How's the weather, {wake_title}?\""
        return f"\"How's the weather in [your city], {wake_title}?\""

    def run(self) -> None:
        """Run capture and release Whisper work on every exit path."""
        from ..tools.confirmation import set_result_handler
        set_result_handler(self._deliver_confirmed_result)
        try:
            self._run()
        finally:
            set_result_handler(None)
            if self._transcription_worker_thread is not None:
                self._finish_transcription_worker()

    def _run(self) -> None:
        """Main voice listening loop."""
        if sd is None:
            debug_log("sounddevice not available", "voice")
            print("  ❌ Audio system not available - sounddevice failed to load", flush=True)
            return

        # Verify PortAudio is working by querying devices (catches Windows DLL issues)
        try:
            devices = sd.query_devices()
            input_devices = [d for d in devices if d.get('max_input_channels', 0) > 0]
            debug_log(f"PortAudio initialised: {len(input_devices)} input device(s) found", "voice")
            if not input_devices:
                print("  ❌ No microphone found. Please connect a microphone.", flush=True)
                return
        except Exception as e:
            debug_log(f"PortAudio device query failed: {e}", "voice")
            print(f"  ❌ Audio system error: {e}", flush=True)
            print("     PortAudio may not be properly installed", flush=True)
            if sys.platform == 'linux':
                print("     On Linux, ensure PortAudio is installed: sudo apt install libportaudio2", flush=True)
            return

        # Resolve the same input for the permission probe and continuous capture.
        try:
            stream_kwargs = resolve_input_device(sd, self.cfg.voice_device, devices)
        except ValueError as exc:
            print(f'  ❌ {exc}', flush=True)
            debug_log('configured input device did not match an available microphone', 'voice')
            return

        # Windows 11: Test microphone permission by attempting a brief recording
        # This catches privacy settings that silently block audio access.
        # A 5-second timeout prevents indefinite hangs when Windows blocks
        # the audio device at the system level without raising an error.
        # Uses InputStream (not sd.rec) so the stream can be explicitly closed
        # on timeout, avoiding resource leaks that could block later audio init.
        if sys.platform == 'win32':
            try:
                print("  🔐 Checking microphone permission...", flush=True)
                mic_ok = threading.Event()
                mic_error: list = [None]

                def _mic_check():
                    # Deliberately NOT under portaudio_lock: this probe's
                    # open/start can hang indefinitely when Windows blocks
                    # mic access at the system level (that is what the 5s
                    # timeout below is for), and hanging while holding the
                    # process-wide lock would freeze every other audio user
                    # (listener, dictation, TTS). The probe runs once at
                    # startup before the listener's main stream opens, so
                    # the residual open/open race is minimal; the quick
                    # stop/close after a successful start stays guarded.
                    stream = None
                    try:
                        stream, _, _ = open_input_stream(
                            sd, self._samplerate, 100, stream_kwargs, serialise=False,
                        )
                        stream.start()
                        time.sleep(0.15)
                        with portaudio_lock:
                            stream.stop()
                            stream.close()
                        stream = None
                        mic_ok.set()
                    except Exception as exc:
                        mic_error[0] = exc
                        if stream is not None:
                            try:
                                with portaudio_lock:
                                    stream.close()
                            except Exception:
                                pass

                check_thread = threading.Thread(target=_mic_check, daemon=True)
                check_thread.start()
                check_thread.join(timeout=5.0)

                if check_thread.is_alive():
                    # Do NOT abort/close the stream from this thread: the
                    # check thread may still be blocked inside start()/stop()
                    # on it, and closing a stream under another thread's feet
                    # is a native use-after-free that aborts the whole app on
                    # Windows (#401). Abandon it — the daemon check thread
                    # will finish the stop/close itself if it ever unblocks.
                    debug_log("microphone permission check timed out after 5s", "voice")
                    print("  ⚠️  Microphone permission check timed out", flush=True)
                    print("     This may indicate Windows is blocking microphone access.", flush=True)
                    print("     Continuing anyway — voice input may not work.", flush=True)
                elif mic_error[0] is not None:
                    e = mic_error[0]
                    error_str = str(e).lower()
                    print(f"  ❌ Microphone permission check failed: {e}", flush=True)
                    if "unapproved" in error_str or "denied" in error_str or "access" in error_str or "-9999" in str(e):
                        print("", flush=True)
                        print("  ┌─────────────────────────────────────────────────────────┐", flush=True)
                        print("  │  🔒 MICROPHONE ACCESS BLOCKED BY WINDOWS               │", flush=True)
                        print("  │                                                         │", flush=True)
                        print("  │  To fix this:                                          │", flush=True)
                        print("  │  1. Open Windows Settings                              │", flush=True)
                        print("  │  2. Go to Privacy & security → Microphone              │", flush=True)
                        print("  │  3. Turn ON 'Microphone access'                        │", flush=True)
                        print("  │  4. Turn ON 'Let apps access your microphone'          │", flush=True)
                        print("  │  5. Turn ON 'Let desktop apps access your microphone'  │", flush=True)
                        print("  │                                                         │", flush=True)
                        print("  │  Then restart Jarvis.                                  │", flush=True)
                        print("  └─────────────────────────────────────────────────────────┘", flush=True)
                        print("", flush=True)
                    return
                elif mic_ok.is_set():
                    print("  ✅ Microphone permission OK", flush=True)
                else:
                    print("  ⚠️  Microphone returned empty audio", flush=True)
            except Exception as e:
                debug_log(f"microphone permission check error: {e}", "voice")
                print(f"  ⚠️  Microphone check error: {e}", flush=True)

        # Kick off LLM warmups in parallel with Whisper load so the first
        # user engagement doesn't pay cold-load cost on either model. All
        # warmup output (Whisper + LLMs) is indented under this header to
        # visually group the phase.
        print("  🔥 Warming up models...", flush=True)
        self._llm_warmup_started_at = time.time()
        self._llm_warmup_threads = self._start_llm_warmup()

        # Determine and initialise Whisper backend
        self._whisper_backend = self._determine_whisper_backend()
        model_name = getattr(self.cfg, "whisper_model", "small")

        # Validate large-v3-turbo support for faster-whisper backend
        if model_name == "large-v3-turbo" and self._whisper_backend != "mlx":
            if not _is_faster_whisper_turbo_supported():
                debug_log(
                    "faster-whisper does not support large-v3-turbo, "
                    "falling back to medium", "voice",
                )
                print(
                    "  ⚠️  large-v3-turbo is not supported by the installed Whisper engine, "
                    "using medium instead. Change the model in Whisper settings "
                    "or rerun the Setup Wizard.", flush=True,
                )
                model_name = "medium"

        if self._whisper_backend == "mlx":
            if not MLX_WHISPER_AVAILABLE:
                debug_log("MLX Whisper not available", "voice")
                print("  ❌ MLX Whisper not available. Install with: pip install mlx-whisper", flush=True)
                return

            self._mlx_model_repo = _get_mlx_model_repo(model_name)
            print(f"🎤 Preparing Whisper '{model_name}' (Apple Silicon GPU)...", flush=True)

            max_retries = 4
            for attempt in range(max_retries + 1):
                try:
                    from .model_download import prepare_mlx_model
                    # Use the same local path for warmup and subsequent transcriptions
                    # so mlx-whisper reuses its in-memory model cache.
                    self._mlx_model_repo = prepare_mlx_model(_get_mlx_model_repo(model_name))
                    # Pre-load the model by doing a warmup transcription.
                    # Use low-amplitude noise (not silence) so the decoder actually runs —
                    # silent audio trips the no-speech short-circuit and leaves the decode
                    # path cold, so the first real utterance still pays the full cost.
                    if np is not None:
                        rng = np.random.default_rng(0)
                        warmup_audio = rng.standard_normal(self._samplerate).astype(np.float32) * 0.01
                        _ = mlx_whisper.transcribe(
                            warmup_audio,
                            path_or_hf_repo=self._mlx_model_repo,
                            language=None,
                        )
                        debug_log(f"MLX Whisper model pre-loaded: repo={self._mlx_model_repo}", "voice")

                    print(f"     🎤 MLX Whisper '{model_name}' ready (Apple Silicon GPU)", flush=True)
                    break
                except Exception as e:
                    error_str = str(e).lower()
                    is_rate_limited = (
                        any(x in error_str for x in ["429", "too many requests", "rate limit"])
                        or getattr(getattr(e, "response", None), "status_code", None) == 429
                    )
                    if is_rate_limited and attempt < max_retries:
                        wait = 2 ** (attempt + 1)
                        debug_log(f"rate limited loading MLX Whisper (attempt {attempt + 1}): {e}", "voice")
                        print(f"  ⏳ Rate limited by HuggingFace, retrying in {wait}s ({attempt + 1}/{max_retries})...", flush=True)
                        time.sleep(wait)
                        continue
                    debug_log(f"failed to initialise MLX Whisper: {e}", "voice")
                    print(f"  ❌ Failed to initialise MLX Whisper: {e}", flush=True)
                    if is_rate_limited:
                        print("  💡 HuggingFace is rate limiting downloads. Please wait a few minutes and restart.", flush=True)
                    return
        else:
            # faster-whisper backend
            WhisperModel = _faster_whisper_model_class()
            if WhisperModel is None:
                debug_log("faster-whisper not available", "voice")
                print("  ❌ faster-whisper not available. Install with: pip install faster-whisper", flush=True)
                return

            device = getattr(self.cfg, "whisper_device", "auto")
            compute = getattr(self.cfg, "whisper_compute_type", "int8")

            # On Windows, probe for CUDA runtime libraries before trying to
            # use them. faster-whisper/CTranslate2 lazily loads cuBLAS and
            # cuDNN during transcription, so without this check a model
            # that loaded fine on cuda will crash on the first audio chunk.
            resolved_device, missing_libs = _probe_windows_cuda_libraries(device)
            if missing_libs:
                _print_cuda_unavailable_hint(missing_libs)
            device = resolved_device

            # Build list of (device, compute_type) combinations to try
            # This handles both compute type fallbacks and CUDA -> CPU fallbacks
            configs_to_try = []

            # Start with preferred config
            compute_types = [compute]
            if compute == "int8":
                compute_types.extend(["float16", "float32"])
            elif compute == "float16":
                compute_types.append("float32")

            # Add preferred device with all compute types
            for ct in compute_types:
                configs_to_try.append((device, ct))

            # If device is "auto" or "cuda", add CPU fallback configs
            # This handles Windows without CUDA libraries
            if device in ("auto", "cuda"):
                for ct in compute_types:
                    configs_to_try.append(("cpu", ct))

            last_error = None
            used_device = device
            used_compute = compute
            for try_device, try_compute in configs_to_try:
                try:
                    cpu_threads = (os.cpu_count() or 4) if try_device in ("cpu", "auto") else 0
                    print(f"     🎤 Loading Whisper '{model_name}' (device={try_device}, compute={try_compute})...", flush=True)
                    self.model = WhisperModel(
                        model_name, device=try_device, compute_type=try_compute,
                        cpu_threads=cpu_threads,
                    )
                    self._apply_whisper_load_success(
                        model_name, try_device, try_compute,
                        device, compute, cpu_threads,
                    )
                    used_device = try_device
                    used_compute = try_compute
                    last_error = None
                    break
                except Exception as e:
                    last_error = e
                    error_str = str(e).lower()

                    # Check if this is a CUDA/GPU-related error that we should fall back from
                    is_cuda_error = any(x in error_str for x in [
                        "cuda", "cublas", "cudnn", "gpu", "nvidia",
                        ".dll is not found", "library", "ctypes"
                    ])
                    is_compute_error = any(x in error_str for x in [
                        "compute type", "int8", "float16"
                    ])

                    if is_cuda_error or is_compute_error:
                        debug_log(f"config ({try_device}, {try_compute}) failed, trying fallback: {e}", "voice")
                        continue

                    # Check for corrupted model cache (e.g. interrupted download)
                    is_corrupted_cache = "unable to open file" in error_str

                    if is_corrupted_cache:
                        debug_log(f"detected corrupted Whisper model cache: {e}", "voice")
                        print("  ⚠️  Whisper model cache appears corrupted, attempting recovery...", flush=True)

                        cache_cleared = _clear_corrupted_whisper_cache(str(e))
                        if cache_cleared:
                            try:
                                print(f"     🎤 Re-downloading Whisper '{model_name}'...", flush=True)
                                self.model = WhisperModel(
                                    model_name, device=try_device, compute_type=try_compute,
                                    cpu_threads=cpu_threads,
                                )
                                self._apply_whisper_load_success(
                                    model_name, try_device, try_compute,
                                    device, compute, cpu_threads,
                                    context="recovered",
                                )
                                used_device = try_device
                                used_compute = try_compute
                                last_error = None
                                break
                            except Exception as retry_e:
                                debug_log(f"retry after cache clear also failed: {retry_e}", "voice")
                                print(f"  ❌ Failed to load Whisper model after cache recovery: {retry_e}", flush=True)
                                debug_log("trying next device/compute fallback config", "voice")
                                continue
                        else:
                            debug_log("could not clear corrupted cache automatically", "voice")
                            print(f"  ❌ Failed to load Whisper model: {e}", flush=True)
                            print("  💡 Try manually deleting the Whisper model cache directory and restarting", flush=True)
                            continue
                    # Check for rate limiting (HTTP 429) — check string and response status code
                    # (HfHubHTTPError may carry the status on .response without "429" in str(e))
                    is_rate_limited = (
                        any(x in error_str for x in ["429", "too many requests", "rate limit"])
                        or getattr(getattr(e, "response", None), "status_code", None) == 429
                    )

                    if is_rate_limited:
                        _max_retries = 4
                        _backoff = 2
                        debug_log(f"rate limited loading Whisper model: {e}", "voice")
                        retry_succeeded = False
                        for retry_num in range(1, _max_retries + 1):
                            wait = _backoff ** retry_num
                            print(f"  ⏳ Rate limited by HuggingFace, retrying in {wait}s ({retry_num}/{_max_retries})...", flush=True)
                            time.sleep(wait)
                            try:
                                self.model = WhisperModel(
                                    model_name, device=try_device, compute_type=try_compute,
                                    cpu_threads=cpu_threads,
                                )
                                self._apply_whisper_load_success(
                                    model_name, try_device, try_compute,
                                    device, compute, cpu_threads,
                                    context="rate-limit retry",
                                )
                                used_device = try_device
                                used_compute = try_compute
                                last_error = None
                                retry_succeeded = True
                                break
                            except Exception as retry_e:
                                debug_log(f"rate-limit retry {retry_num} failed: {retry_e}", "voice")
                                last_error = retry_e
                        if retry_succeeded:
                            break
                        debug_log(f"gave up after {_max_retries} rate-limit retries", "voice")
                        print(f"  ❌ Failed to load Whisper model after {_max_retries} retries: {last_error}", flush=True)
                        print("  💡 HuggingFace is rate limiting downloads. Please wait a few minutes and restart.", flush=True)
                        return
                    else:
                        # For other errors (model not found, etc.), don't try fallbacks
                        debug_log(f"failed to initialise faster-whisper: {e}", "voice")
                        print(f"  ❌ Failed to load Whisper model: {e}", flush=True)
                        return

            if last_error is not None:
                debug_log(f"failed to initialise faster-whisper with any config: {last_error}", "voice")
                print(f"  ❌ Failed to load Whisper model: {last_error}", flush=True)
                return

            # Warm up faster-whisper so the first real utterance doesn't pay
            # the cold-decode cost. Use low-amplitude noise rather than pure
            # silence — silence trips faster-whisper's no-speech short-circuit
            # and the decoder never actually runs. Mirror the real transcribe
            # parameters so beam search, language detection, and the timestamp
            # path are all exercised here instead of on the user's first word.
            if np is not None and self.model is not None:
                try:
                    cpu_mode = self._whisper_device == "cpu"
                    rng = np.random.default_rng(0)
                    warmup_audio = rng.standard_normal(self._samplerate).astype(np.float32) * 0.01
                    try:
                        segments_iter, _ = self.model.transcribe(
                            warmup_audio,
                            language=None,
                            vad_filter=False,
                            condition_on_previous_text=not cpu_mode,
                            without_timestamps=cpu_mode,
                        )
                    except TypeError:
                        segments_iter, _ = self.model.transcribe(warmup_audio, language=None)
                    for _ in segments_iter:
                        pass
                    debug_log("faster-whisper warmup transcription complete", "voice")
                except Exception as e:
                    debug_log(f"faster-whisper warmup failed: {e}", "voice")

        # Wait for LLM probes before announcing "Listening!". A single
        # 60s budget is shared across
        # all warmup threads so a slow/down Ollama can't block us from
        # listening — we'll just pay the cold-load cost on demand.
        warmup_threads = getattr(self, "_llm_warmup_threads", [])
        if warmup_threads:
            budget = 60.0
            deadline = getattr(self, "_llm_warmup_started_at", time.time()) + budget
            for t in warmup_threads:
                remaining = max(0.0, deadline - time.time())
                t.join(timeout=remaining)

            still_warming = any(t.is_alive() for t in warmup_threads)
            self._report_llm_warmup()

            if still_warming:
                debug_log("LLM warmup still running after 60s — continuing without", "voice")
                print("     ⏳ Some model probes are still running; continuing startup.", flush=True)

        # Audio parameters
        frame_ms = self._configure_audio(int(getattr(self.cfg, "vad_frame_ms", 20)))
        debug_log(f"audio params: sample_rate={self._samplerate}, frame_ms={frame_ms}, frame_samples={self._frame_samples}", "voice")
        debug_log(f"VAD: enabled={bool(self._vad is not None)}, aggressiveness={getattr(self.cfg, 'vad_aggressiveness', 2)}", "voice")

        # Audio device setup
        if self.cfg.voice_debug:
            debug_log("available input devices:", "voice")
            try:
                for idx, dev in enumerate(sd.query_devices()):
                    try:
                        max_in = int(dev.get("max_input_channels", 0))
                    except Exception:
                        max_in = 0
                    if max_in > 0:
                        name = dev.get("name")
                        rate = dev.get("default_samplerate")
                        debug_log(f"  [{idx}] {name} (channels={max_in}, default_sr={rate})", "voice")
            except Exception:
                pass

        # Log which device will be used
        try:
            if "device" in stream_kwargs:
                dev = sd.query_devices(stream_kwargs["device"])
                device_name = dev.get('name', 'Unknown')
                debug_log(f"using input device: {device_name} (index {stream_kwargs['device']})", "voice")
                print(f"  🎤 Using audio device: {device_name}", flush=True)
            else:
                debug_log("using system default input device", "voice")
                try:
                    default_dev = sd.query_devices(sd.default.device[0])
                    print(f"  🎤 Using default device: {default_dev.get('name', 'Unknown')}", flush=True)
                except Exception:
                    print("  🎤 Using system default input device", flush=True)
        except Exception:
            pass

        self._stream_samplerate = self._samplerate
        open_error = None
        try:
            stream, self._stream_samplerate, channels = open_input_stream(
                sd, self._samplerate, frame_ms, stream_kwargs, callback=self._on_audio,
            )
            self._frame_samples = max(1, int(self._stream_samplerate * frame_ms / 1000))
            if self._stream_samplerate != self._samplerate or channels != 1:
                print(f"  🎤 Using {self._stream_samplerate} Hz, {channels} input channel(s); "
                      "converting to mono and resampling for speech recognition", flush=True)
        except Exception as e:
            open_error = e

        if open_error is not None:
            error_msg = str(open_error).lower()
            debug_log(f"failed to open input stream: {open_error}", "voice")

            # Provide helpful error messages for common issues
            if "access" in error_msg or "permission" in error_msg:
                print(f"  ❌ Microphone access denied. Please check: {_get_mic_permission_hint()}", flush=True)
            elif "device" in error_msg and ("use" in error_msg or "busy" in error_msg):
                print("  ❌ Microphone is being used by another application", flush=True)
            elif "device" in error_msg:
                print(f"  ❌ Failed to open microphone: {open_error}", flush=True)
                print("     Try selecting a different audio device in settings", flush=True)
            else:
                print(f"  ❌ Failed to start audio recording: {open_error}", flush=True)
            return

        self._pending_audio = None
        self._callback_count = 0
        self._reset_audio_health()
        debug_log(f"Capture stream: {self._stream_samplerate} Hz, {self._frame_samples} samples per {frame_ms} ms frame", "voice")
        # Main audio processing loop
        with _serialised_stream(stream):
            # Verify stream is actually recording (helps catch permission issues)
            if not stream.active:
                try:
                    with portaudio_lock:
                        stream.start()
                except Exception as e:
                    error_msg = str(e).lower()
                    debug_log(f"failed to start audio stream: {e}", "voice")
                    if "access" in error_msg or "permission" in error_msg:
                        print(f"  ❌ Microphone access denied. Please check: {_get_mic_permission_hint()}", flush=True)
                    else:
                        print(f"  ❌ Failed to start recording: {e}", flush=True)
                    return

            self._start_transcription_worker()

            # Show ready message only after stream is confirmed active
            wake_word = getattr(self.cfg, "wake_word", "jarvis").lower()
            wake_title = wake_word.title()
            print(f"\n{'─' * 50}\n🎙️  Listening! Try:", flush=True)
            print(f"      {self._weather_example(wake_title)}", flush=True)
            print(f"      \"I just ate a Big Mac, {wake_title}.\"", flush=True)
            print(f"      \"What are you thinking, {wake_title}?\"", flush=True)
            print(f"      \"What do you know about me, {wake_title}?\"", flush=True)

            # Small-model disclaimer: SMALL models can't infer your intent
            # from vague prompts, but they can still execute complex flows
            # if you spell out the steps. Assume the model is dumb and lay
            # things out for it. Classification lives in model_variants so
            # it stays in sync when supported models change.
            from ..reply.prompts.model_variants import detect_model_size, ModelSize
            chat_model_name = str(getattr(self.cfg, "llm_chat_model", "") or "").strip()
            if chat_model_name and detect_model_size(chat_model_name) == ModelSize.SMALL:
                print(
                    f"  ⚠️  Small model in use ({chat_model_name}). Assume it can't infer — spell out the steps for anything more involved:",
                    flush=True,
                )
                print(
                    f"      \"Tell me tomorrow's weather, then find local events for tomorrow, then recommend ones that suit the weather, {wake_title}.\"",
                    flush=True,
                )

            # Chrome MCP tip: the chrome MCP exposes a `navigate` tool that
            # takes a URL. Vague phrasing like "Open YouTube" forces the model
            # to guess a URL; "Navigate to youtube.com" maps directly to the
            # tool's argument and is more reliable on small models.
            try:
                from ..tools.registry import get_cached_mcp_tools
                mcp_tool_names = list(get_cached_mcp_tools(wait=False).keys())
                has_chrome_mcp = any("chrome" in name.lower() for name in mcp_tool_names)
            except Exception:
                has_chrome_mcp = False
            if has_chrome_mcp:
                print(
                    f"  🌐 Chrome MCP detected. Name the destination URL so the browser tool can act directly:",
                    flush=True,
                )
                print(
                    f"      \"Navigate to youtube.com, {wake_title}.\"",
                    flush=True,
                )

            # Awake and ready, waiting for the wake word
            set_state(AssistantState.IDLE)

            dictation_paused = False
            while not self._should_stop:
                if self._dictation_active:
                    if not dictation_paused:
                        self._clear_audio_buffers()
                        dictation_paused = True
                    while True:
                        try:
                            self._transcription_results_q.get_nowait()
                        except queue.Empty:
                            break
                    self._check_audio_health()
                    time.sleep(0.05)
                    continue
                dictation_paused = False
                try:
                    result = self._transcription_results_q.get_nowait()
                except queue.Empty:
                    pass
                else:
                    self._handle_transcription_result(result)

                if self._should_stop or self._dictation_active:
                    continue

                self._check_audio_health()

                try:
                    item = self._audio_q.get(timeout=0.2)
                except queue.Empty:
                    # Critical: Check timeouts even when no audio is being received
                    # This ensures hot window expiry fires reliably
                    self._check_query_timeout()
                    continue

                if self._should_stop or self._dictation_active:
                    continue

                if item is None:
                    # Reset marker
                    self._pending_audio = None
                    self.is_speech_active = False
                    self._silence_frames = 0
                    self._utterance_frames = []
                    self._pre_roll.clear()
                    continue

                if np is None:
                    continue

                self._process_audio_block(item)

    def _configure_audio(self, frame_ms: int) -> int:
        """Derive frame-based VAD limits from config; returns the frame duration used."""
        if frame_ms not in (10, 20, 30):
            debug_log(f"Unsupported VAD frame duration {frame_ms}; using 20 ms", "voice")
            frame_ms = 20
        self._frame_ms = frame_ms
        rate = getattr(self, "_stream_samplerate", self._samplerate)
        self._frame_samples = max(1, int(rate * frame_ms / 1000))
        self._pre_roll_max_frames = max(1, int(int(getattr(self.cfg, "vad_pre_roll_ms", 240)) / frame_ms))
        self._endpoint_silence_frames = max(1, int(int(getattr(self.cfg, "endpoint_silence_ms", 600)) / frame_ms))
        # The utterance length limit depends on TTS state at the time of each frame.
        self._normal_max_utt_frames = max(1, int(int(getattr(self.cfg, "max_utterance_ms", 12000)) / frame_ms))
        self._tts_max_utt_frames = max(1, int(int(getattr(self.cfg, "tts_max_utterance_ms", 3000)) / frame_ms))
        return frame_ms

    def _process_audio_block(self, item) -> None:
        """Run VAD and utterance assembly over one captured audio block."""
        frame_timestamp = time.time()  # Timestamp for this batch of frames
        for frame in self._audio_frames(item):
            # VAD decision
            is_voice = self._is_speech_frame(frame)
            self._speech_frames_seen += int(is_voice)
            if is_voice:
                self._last_voice_frame_time = frame_timestamp

            if not self.is_speech_active:
                if is_voice:
                    self.is_speech_active = True

                    # Backdate start time by pre-roll duration — the
                    # actual speech onset was before VAD triggered.
                    pre_roll_sec = len(self._pre_roll) * self._frame_ms / 1000.0
                    utterance_start_time = time.time() - pre_roll_sec

                    # Track utterance timing for echo detection
                    self.echo_detector.track_utterance_timing(utterance_start_time, 0.0)

                    # Seed with pre-roll
                    if self._pre_roll:
                        self._utterance_frames.extend(list(self._pre_roll))
                    self._utterance_frames.append(frame.copy())
                    self._silence_frames = 0
                else:
                    # Maintain pre-roll buffer
                    self._pre_roll.append(frame.copy())
                    while len(self._pre_roll) > self._pre_roll_max_frames:
                        try:
                            self._pre_roll.popleft()
                        except Exception:
                            break
            else:
                if is_voice:
                    self._utterance_frames.append(frame.copy())
                    self._silence_frames = 0
                else:
                    self._silence_frames += 1
                    # Use shorter timeout during TTS for quick stop command detection
                    current_max_frames = self._tts_max_utt_frames if (self.tts and self.tts.is_speaking()) else self._normal_max_utt_frames
                    if self._silence_frames >= self._endpoint_silence_frames or len(self._utterance_frames) >= current_max_frames:
                        self._finalize_utterance()
                        self._pre_roll.clear()

            self._barge_in_tick(is_voice)

            # Check for query timeouts
            self._check_query_timeout()

    def _finalize_utterance(self) -> None:
        """Queue a completed utterance for serial transcription."""
        self._barge_in_voiced_frames = 0
        self._barge_in_next_check = 0
        self._barge_in_passes = 0
        if self._should_stop or self._dictation_active:
            self.is_speech_active = False
            self._silence_frames = 0
            self._utterance_frames = []
            return
        if np is None or not self._utterance_frames:
            self.is_speech_active = False
            self._silence_frames = 0
            self._utterance_frames = []
            return

        # Track when utterance ends - but don't overwrite global timing yet
        utterance_end_time = time.time()
        utterance_start_time = self.echo_detector._utterance_start_time
        speech_end_time = self._last_voice_frame_time or utterance_end_time
        TurnLatency(speech_end_time).mark("utterance finalised (VAD endpoint silence)")

        if self.cfg.voice_debug:
            utterance_duration = utterance_end_time - utterance_start_time if utterance_start_time > 0 else 0
            start_time_str = datetime.fromtimestamp(utterance_start_time).strftime('%H:%M:%S.%f')[:-3] if utterance_start_time > 0 else "N/A"
            end_time_str = datetime.fromtimestamp(utterance_end_time).strftime('%H:%M:%S.%f')[:-3]
            debug_log(f"utterance captured: duration={utterance_duration:.2f}s (started: {start_time_str}, ended: {end_time_str})", "voice")

        # The intent judge extracts the relevant query from the full utterance.
        try:
            audio = np.concatenate(self._utterance_frames, axis=0).flatten()
        except Exception:
            audio = None

        # Calculate energy before clearing frames for transcript processing
        utterance_energy = self._calculate_audio_energy(self._utterance_frames[-10:] if self._utterance_frames else [])

        # Reset state before processing
        self.is_speech_active = False
        self._silence_frames = 0
        self._utterance_frames = []

        if audio is None or audio.size == 0:
            return

        # Resample to Whisper's expected rate if the stream ran at a different rate
        stream_rate = getattr(self, "_stream_samplerate", self._samplerate)
        if stream_rate != self._samplerate:
            audio = _resample(audio, stream_rate, self._samplerate)

        # Filter short audio
        audio_duration = len(audio) / self._samplerate
        min_duration = getattr(self.cfg, "whisper_min_audio_duration", 0.3)
        if audio_duration < min_duration:
            debug_log(f"audio too short ({audio_duration:.2f}s < {min_duration}s), ignoring", "voice")
            self.state_manager.check_hot_window_expiry(self.cfg.voice_debug)
            return

        job = _TranscriptionJob(
            audio=audio,
            start_time=utterance_start_time,
            end_time=utterance_end_time,
            speech_end_time=speech_end_time,
            energy=utterance_energy,
            dictation_generation=self._dictation_generation,
            captured_during_tts=(
                self.echo_detector._tts_start_time > 0
                and utterance_end_time >= self.echo_detector._tts_start_time
                and (
                    (self.tts is not None and self.tts.is_speaking())
                    or utterance_start_time < (
                        self.echo_detector._last_tts_finish_time
                        + self.echo_detector.echo_tolerance
                    )
                )
            ),
            captured_tts_start_time=self.echo_detector._tts_start_time,
        )
        try:
            self._transcription_jobs_q.put_nowait(job)
        except queue.Full:
            debug_log("transcription backlog full; utterance discarded", "voice")
            print("  ⚠️  Whisper is behind; this utterance was not transcribed.", flush=True)
            self.state_manager.check_hot_window_expiry(self.cfg.voice_debug)

    def _transcribe_audio(self, audio) -> tuple[str, Optional[str], tuple[LowConfidenceEvent, ...]]:
        """Run Whisper and return filtered text, language and rejection events."""
        detected = None
        low_confidence_events = []
        try:
            if self._whisper_backend == "mlx":
                # MLX Whisper transcription
                with self.transcribe_lock:
                    result = mlx_whisper.transcribe(
                        audio,
                        path_or_hf_repo=self._mlx_model_repo,
                        language=None,
                    )

                # Capture Whisper's auto-detected language (ISO-639-1) so
                # downstream tools can pick locale-appropriate resources.
                detected = result.get("language")

                # Filter segments by confidence (MLX Whisper returns segments with avg_logprob)
                min_confidence = getattr(self.cfg, "whisper_min_confidence", 0.3)
                marginal_threshold = min_confidence / 3  # Show user-visible log for marginal confidence
                no_speech_threshold = getattr(self.cfg, "whisper_no_speech_threshold", 0.5)
                segments = result.get("segments", [])

                if segments:
                    filtered_texts = []
                    for seg in segments:
                        avg_logprob = seg.get("avg_logprob", 0)
                        no_speech_prob = seg.get("no_speech_prob", 0)

                        # Convert avg_logprob to confidence (typically -1 to 0, so add 1)
                        confidence = min(1.0, max(0.0, avg_logprob + 1.0))
                        seg_text = seg.get("text", "").strip()

                        # Hard filter: high no_speech_prob means no real speech regardless of logprob.
                        if is_whisper_hallucination(no_speech_prob, no_speech_threshold):
                            debug_log(f"MLX segment filtered (no_speech_prob={no_speech_prob:.2f}): '{seg_text[:50]}'", "voice")
                            continue

                        if confidence < min_confidence:
                            if self.on_low_confidence is not None:
                                low_confidence_events.append(
                                    LowConfidenceEvent(confidence, seg.get("text", ""))
                                )
                            if confidence >= marginal_threshold:
                                # Marginal confidence - show in log viewer (not debug)
                                print(f"🔇 Low confidence ({confidence:.2f}): \"{seg_text[:50]}...\"", flush=True)
                            else:
                                # Very low confidence - debug only
                                debug_log(f"MLX segment filtered (confidence={confidence:.2f}): '{seg_text[:50]}'", "voice")
                            continue

                        filtered_texts.append(seg.get("text", ""))

                    text = " ".join(filtered_texts).strip()
                else:
                    # Fallback to full text if no segments
                    text = result.get("text", "").strip()
            else:
                # faster-whisper transcription
                # CPU mode: skip timestamps and disable context carry-over for speed
                cpu_mode = self._whisper_device == "cpu"
                with self.transcribe_lock:
                    try:
                        segments, _info = self.model.transcribe(
                            audio, language=None, vad_filter=False,
                            condition_on_previous_text=not cpu_mode,
                            without_timestamps=cpu_mode,
                        )
                    except TypeError:
                        segments, _info = self.model.transcribe(audio, language=None)
                    segments_list = list(segments)
                # Capture the detected language (faster-whisper exposes it
                # on the info object). Guard against older API variants
                # where the attribute may be absent.
                detected = getattr(_info, "language", None)
                filtered_segments, low_confidence_events = self._filter_noisy_segments(segments_list)
                text = " ".join(seg.text for seg in filtered_segments).strip()
        except Exception as e:
            debug_log(f"transcription error: {e}", "voice")
            if sys.platform == 'win32':
                print(f"  ❌ Whisper error: {e}", flush=True)
            return "", None, ()
        return text, detected if isinstance(detected, str) and detected else None, tuple(low_confidence_events)
