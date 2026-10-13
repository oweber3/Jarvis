"""Kokoro's graphics card helper (``output/tts.spec.md``, Graphics card helper).

Kokoro on an NVIDIA card needs ONNX Runtime's CUDA build and NVIDIA's CUDA 13 libraries. Installed into
Jarvis's own environment they would replace the CUDA 12 cuDNN that Whisper loads and the ONNX Runtime Piper
uses, so they live in a separate environment instead, and the model runs in a helper process from it
(``kokoro_gpu_worker.py``). Jarvis keeps the phonemes, voices and audio handling; the helper only runs the
model. It is installed only when the user asks (Settings or ``python -m jarvis.output.kokoro_gpu install``),
from PyPI with pinned versions, and removed with ``remove``.
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

from ..debug import debug_log

# Versions measured together on an RTX 5070 (Blackwell): ONNX Runtime 1.31 is built for CUDA 13.
HELPER_PACKAGES = (
    "onnxruntime-gpu==1.31.0",
    "nvidia-cuda-runtime==13.4.92",
    "nvidia-cudnn-cu13==9.27.0.42",
    "nvidia-cublas==13.8.0.4",
    "nvidia-cufft==12.4.0.43",
    "nvidia-curand==10.4.4.72",
    "numpy==2.3.5",
)
HELPER_DOWNLOAD_GB = 1.9
_READY_MARKER = "jarvis-helper-ready"
_WORKER = Path(__file__).with_name("kokoro_gpu_worker.py")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def helper_dir() -> Path:
    return Path.home() / ".local" / "share" / "jarvis" / "kokoro-gpu"


def helper_python(base: Optional[Path] = None) -> Path:
    base = Path(base or helper_dir())
    return base / "Scripts" / "python.exe" if os.name == "nt" else base / "bin" / "python"


def helper_installed(base: Optional[Path] = None) -> bool:
    """The helper environment exists and passed its graphics card check when it was installed."""
    base = Path(base or helper_dir())
    return helper_python(base).is_file() and (base / _READY_MARKER).is_file()


def helper_supported() -> bool:
    """The helper needs a Python to build its environment from, which a packaged Jarvis does not have."""
    return not getattr(sys, "frozen", False)


def _check_helper(python: Path):
    """``(True, provider)`` when the helper's ONNX Runtime can run on the graphics card, else ``(False, why)``."""
    code = ("import onnxruntime as o\n"
            "o.preload_dlls() if hasattr(o, 'preload_dlls') else None\n"
            "p = o.get_available_providers()\n"
            "print('CUDAExecutionProvider' if 'CUDAExecutionProvider' in p else 'missing CUDA: ' + ','.join(p))\n")
    try:
        done = subprocess.run([str(python), "-I", "-c", code], capture_output=True, text=True, timeout=300,
                              creationflags=_NO_WINDOW)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    answer = (done.stdout or "").strip().splitlines()
    if done.returncode == 0 and answer and answer[-1] == "CUDAExecutionProvider":
        return True, answer[-1]
    return False, (answer[-1] if answer else (done.stderr or "").strip()[-300:]) or "the check failed"


def install_helper(base: Optional[Path] = None, progress: Optional[Callable[[str], None]] = None) -> bool:
    """Build the helper environment and check it can use the graphics card; True when ready."""
    def say(message: str) -> None:
        if progress:
            try:
                progress(message)
            except Exception:
                pass

    base = Path(base or helper_dir())
    if not helper_supported():
        say("❌ Graphics card support for Kokoro needs Jarvis run from source")
        return False
    say(f"📦 Installing Kokoro's graphics card support (about {HELPER_DOWNLOAD_GB} GB, one time)")
    debug_log("Kokoro graphics card helper install started", "tts")
    try:
        steps = [
            ([sys.executable, "-m", "venv", str(base)], "create its environment"),
            ([str(helper_python(base)), "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
              "--only-binary", ":all:", *HELPER_PACKAGES], "download its packages"),
        ]
        for command, what in steps:
            say(f"   ⏳ Working: {what}...")
            done = subprocess.run(command, capture_output=True, text=True, timeout=3600, creationflags=_NO_WINDOW)
            if done.returncode != 0:
                raise RuntimeError(f"could not {what}: {(done.stderr or done.stdout or '').strip()[-300:]}")
        ok, detail = _check_helper(helper_python(base))
        if not ok:
            raise RuntimeError(f"the graphics card could not be used ({detail})")
        (base / _READY_MARKER).write_text(detail, encoding="utf-8")
    except Exception as e:
        say(f"❌ Kokoro's graphics card support was not installed: {e}")
        debug_log(f"Kokoro graphics card helper install failed ({type(e).__name__})", "tts")
        shutil.rmtree(base, ignore_errors=True)
        return False
    say("✅ Kokoro's graphics card support is installed")
    debug_log("Kokoro graphics card helper installed", "tts")
    return True


def remove_helper(base: Optional[Path] = None) -> None:
    shutil.rmtree(Path(base or helper_dir()), ignore_errors=True)
    debug_log("Kokoro graphics card helper removed", "tts")


def _read_exact(stream, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


class WorkerSession:
    """An ONNX session that runs in the helper process; ``run`` and ``get_providers`` follow
    ``onnxruntime.InferenceSession``. Raises ``RuntimeError`` when the helper cannot start or stops."""

    def __init__(self, python, model_path, providers: str = "cuda") -> None:
        self._lock = threading.Lock()
        self._process = subprocess.Popen(
            [str(python), "-I", str(_WORKER), str(model_path), providers],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        self.pid = self._process.pid
        try:
            kind, payload = self._read_reply()
        except RuntimeError:
            self.close()
            raise
        if kind != b"R":
            self.close()
            raise RuntimeError(payload.decode("utf-8", "replace"))
        self._provider = payload.decode()
        debug_log(f"Kokoro graphics card helper started on {self._provider}", "tts")

    def get_providers(self):
        return [self._provider]

    def _read_reply(self):
        try:
            kind = _read_exact(self._process.stdout, 1)
            (size,) = struct.unpack("<I", _read_exact(self._process.stdout, 4))
            return kind, _read_exact(self._process.stdout, size)
        except (EOFError, OSError, ValueError) as e:
            raise RuntimeError("Kokoro's graphics card helper stopped") from e

    def run(self, _outputs, inputs):
        import io
        import numpy as np

        buffer = io.BytesIO()
        np.savez(buffer, **inputs)
        request = buffer.getvalue()
        with self._lock:
            try:
                self._process.stdin.write(struct.pack("<I", len(request)) + request)
                self._process.stdin.flush()
            except (OSError, ValueError) as e:
                raise RuntimeError("Kokoro's graphics card helper stopped") from e
            kind, payload = self._read_reply()
        if kind == b"E":
            raise RuntimeError(payload.decode("utf-8", "replace"))
        with np.load(io.BytesIO(payload)) as reply:
            return [reply["audio"]]

    def close(self) -> None:
        process = getattr(self, "_process", None)
        if process is None or process.poll() is not None:
            return
        try:
            process.stdin.close()
            process.wait(timeout=3)
        except Exception:
            process.kill()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    action = args[0] if args else "status"
    if action == "install":
        return 0 if install_helper(progress=print) else 1
    if action == "remove":
        remove_helper()
        print("🗑️ Kokoro's graphics card support was removed")
        return 0
    if helper_installed():
        print(f"✅ Kokoro's graphics card support is installed in {helper_dir()}")
    else:
        print("ℹ️ Kokoro's graphics card support is not installed")
        print("   Install it with: python -m jarvis.output.kokoro_gpu install")
    return 0


if __name__ == "__main__":
    sys.exit(main())
