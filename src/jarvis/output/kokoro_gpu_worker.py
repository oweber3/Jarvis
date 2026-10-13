"""Kokoro's graphics card helper process (``output/tts.spec.md``, Graphics card helper).

Run by the helper environment's Python as a plain script; it imports only numpy and ONNX Runtime, never
Jarvis. It loads one ONNX model and answers requests on stdin/stdout until stdin closes.

Frames: a request is a 4-byte little-endian length and an ``.npz`` of the model inputs. A reply is one kind
byte and a 4-byte length: ``R`` (ready, the provider name), ``A`` (an ``.npz`` with ``audio``) or ``E`` (an
error message). Nothing is logged and nothing is sent anywhere.
"""
import io
import struct
import sys


def _send(out, kind: bytes, payload: bytes) -> None:
    out.write(kind + struct.pack("<I", len(payload)) + payload)
    out.flush()


def _read_exact(stream, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def main() -> None:
    model_path, providers_choice = sys.argv[1], sys.argv[2]
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    try:
        import numpy as np
        import onnxruntime as ort

        try:
            ort.disable_telemetry_events()
        except Exception:
            pass
        options = ort.SessionOptions()
        options.log_severity_level = 3
        if providers_choice == "cuda":
            if hasattr(ort, "preload_dlls"):
                ort.preload_dlls()
            providers = ["CUDAExecutionProvider"]
        else:
            providers = ["CPUExecutionProvider"]
        session = ort.InferenceSession(model_path, options, providers=providers)
        session.disable_fallback()  # a CUDA failure must surface, not quietly move to the processor
    except Exception as e:
        _send(stdout, b"E", f"could not load the model: {type(e).__name__}: {e}".encode("utf-8", "replace"))
        return
    _send(stdout, b"R", session.get_providers()[0].encode())

    while True:
        try:
            (size,) = struct.unpack("<I", _read_exact(stdin, 4))
            request = _read_exact(stdin, size)
        except EOFError:
            return
        try:
            with np.load(io.BytesIO(request)) as inputs:
                feed = {name: inputs[name] for name in inputs.files}
            audio = session.run(None, feed)[0]
            buffer = io.BytesIO()
            np.savez(buffer, audio=np.asarray(audio, dtype=np.float32))
            _send(stdout, b"A", buffer.getvalue())
        except Exception as e:
            _send(stdout, b"E", f"{type(e).__name__}: {e}".encode("utf-8", "replace"))


if __name__ == "__main__":
    main()
