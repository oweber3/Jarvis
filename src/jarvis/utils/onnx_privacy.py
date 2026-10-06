"""Keep onnxruntime from emitting its own telemetry.

Official onnxruntime builds have telemetry events on by default. Every place
Jarvis creates an onnxruntime session (Piper voices, speaker verification)
calls this first, so nothing about Jarvis's sessions is reported anywhere.
"""

from ..debug import debug_log


def disable_onnxruntime_telemetry() -> None:
    """Switch onnxruntime telemetry off. Never raises; a missing runtime is fine."""
    try:
        import onnxruntime
    except ImportError:
        return
    try:
        onnxruntime.disable_telemetry_events()
    except Exception as e:
        debug_log(f"onnxruntime telemetry switch unavailable: {type(e).__name__}", "privacy")
