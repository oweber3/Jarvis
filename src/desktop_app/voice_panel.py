"""The Speaking page's voice panel (``settings_window.spec.md``, Voice preview).

Shows whether the chosen voice is downloaded, whether Kokoro can use a graphics card (information only; the
engine is never changed for the hardware), and a Preview button that speaks a sample line with the unsaved
choice on a worker thread.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QGroupBox, QLabel, QPushButton, QVBoxLayout

from jarvis.debug import debug_log
from jarvis.output import kokoro
from jarvis.output.piper_voices import PIPER_DEFAULT_VOICE, piper_voice, piper_voice_downloaded
from jarvis.output.voice_preview import SAMPLE_LINE, PreviewUnavailable, play, render_preview

PREVIEW_KEYS = (
    "tts_engine", "tts_piper_voice", "tts_piper_model_path", "tts_piper_speaker", "tts_piper_length_scale",
    "tts_piper_noise_scale", "tts_piper_noise_w", "tts_kokoro_voice", "tts_kokoro_speed", "tts_kokoro_device",
)
ENGINE_NAMES = {"piper": "Piper", "kokoro": "Kokoro", "chatterbox": "Chatterbox"}


def _nvidia_card_present() -> bool:
    try:
        from desktop_app.cuda_recovery import _has_nvidia_driver
        return _has_nvidia_driver()
    except Exception:
        return False


class VoicePanel(QGroupBox):
    """Download state, graphics card information and the voice preview."""

    progress = pyqtSignal(str)
    finished = pyqtSignal(object)

    def __init__(self, current: Callable[[str], Any], assistant_name: Callable[[], str]) -> None:
        super().__init__("Voice preview")
        self._current = current
        self._assistant_name = assistant_name
        self._busy = False
        self._gpu_line: Optional[str] = None
        layout = QVBoxLayout(self)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.preview_button = QPushButton("Preview")
        self.preview_button.clicked.connect(self._start_preview)
        layout.addWidget(self.preview_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        privacy = QLabel("Voices run on this PC. Voice files download once from fixed addresses, are checked "
                         "before use and then work offline; nothing you say is sent anywhere.")
        privacy.setObjectName("hint")
        privacy.setWordWrap(True)
        layout.addWidget(privacy)
        self.progress.connect(self._on_progress)
        self.finished.connect(self._on_finished)

    def _graphics_card_line(self) -> str:
        if self._gpu_line is None:
            if kokoro.gpu_acceleration_available():
                self._gpu_line = "Graphics card: available, so Kokoro runs on it."
            elif _nvidia_card_present():
                self._gpu_line = ("Graphics card: an NVIDIA card was found, but this install runs Kokoro on the "
                                  "processor (the graphics card build of ONNX Runtime is not installed).")
            else:
                self._gpu_line = "Graphics card: none Kokoro can use, so it runs on the processor."
        return self._gpu_line

    @pyqtSlot()
    def refresh(self, *_args) -> None:
        engine = str(self._current("tts_engine") or "piper")
        lines = []
        button = "Preview"
        can_preview = engine in ("piper", "kokoro")
        if engine == "piper":
            custom = self._current("tts_piper_model_path")
            if custom:
                lines.append(f"Custom voice file: {str(custom).replace(chr(92), '/').rsplit('/', 1)[-1]}")
            else:
                key = str(self._current("tts_piper_voice") or PIPER_DEFAULT_VOICE)
                voice = piper_voice(key)
                if voice is not None and piper_voice_downloaded(key):
                    lines.append(f"{voice.label}: downloaded.")
                elif voice is not None:
                    lines.append(f"{voice.label}: not downloaded yet ({voice.download_mb} MB, one time).")
                    button = f"Download and preview ({voice.download_mb} MB)"
        elif engine == "kokoro":
            if kokoro.kokoro_files_present():
                lines.append("Kokoro voice files: downloaded.")
            else:
                lines.append(f"Kokoro voice files: not downloaded yet ({kokoro.KOKORO_DOWNLOAD_MB} MB, one time). "
                             "Until they are, Piper speaks.")
                button = f"Download and preview ({kokoro.KOKORO_DOWNLOAD_MB} MB)"
            lines.append(self._graphics_card_line())
        else:
            lines.append("Preview is available for Piper and Kokoro voices.")
        self.status_label.setText("\n".join(lines))
        self.preview_button.setText(button)
        self.preview_button.setEnabled(can_preview and not self._busy)

    def _start_preview(self) -> None:
        if self._busy:
            return
        values = {key: self._current(key) for key in PREVIEW_KEYS}
        text = SAMPLE_LINE.format(name=self._assistant_name())
        self._busy = True
        self.preview_button.setEnabled(False)
        self.result_label.setText("Preparing the voice...")
        debug_log(f"Voice preview requested for {values.get('tts_engine')}", "settings")

        def work() -> None:
            try:
                result = render_preview(values, text, self._emit_progress)
                self._emit("finished", ("playing", result))
                play(result)
            except PreviewUnavailable as e:
                result = e
            except Exception as e:  # noqa: BLE001 - the button must come back whatever happened
                debug_log(f"Voice preview failed ({type(e).__name__})", "settings")
                result = PreviewUnavailable(f"The preview failed ({type(e).__name__}).")
            self._emit("finished", result)

        threading.Thread(target=work, daemon=True, name="settings-voice-preview").start()

    def _emit(self, signal_name: str, value) -> None:
        """Emit from the worker thread; once the window has closed (its Qt object is gone) nothing is shown."""
        try:
            getattr(self, signal_name).emit(value)
        except RuntimeError:
            pass

    def _emit_progress(self, message: str) -> None:
        self._emit("progress", message)

    @pyqtSlot(str)
    def _on_progress(self, message: str) -> None:
        self.result_label.setText(message.strip().replace("||", ""))

    @pyqtSlot(object)
    def _on_finished(self, outcome) -> None:
        if isinstance(outcome, tuple):  # synthesis done, now playing
            _, result = outcome
            self.result_label.setText(self._describe(result, playing=True))
            return
        self._busy = False
        if isinstance(outcome, PreviewUnavailable):
            self.result_label.setText(str(outcome))
        else:
            self.result_label.setText(self._describe(outcome, playing=False))
        self.refresh()

    @staticmethod
    def _describe(result, playing: bool) -> str:
        name = ENGINE_NAMES.get(result.engine, result.engine)
        text = f"{'Playing' if playing else 'Played'} with {name}."
        if result.note:
            text += f" Kokoro could not speak ({result.note}), so Piper stood in."
        return text
