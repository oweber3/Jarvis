"""Kokoro's graphics card helper: a separate process with its own ONNX Runtime (CUDA 13), so Jarvis's own
libraries (Whisper's CUDA 12 cuDNN, Piper's ONNX Runtime) are never replaced.

The round-trip tests run the real helper script with this interpreter and a tiny ONNX model on the processor;
nothing here installs anything or needs a graphics card.
"""
import sys

import numpy as np
import pytest

from jarvis.output import kokoro, kokoro_gpu

onnx = pytest.importorskip("onnx")
pytest.importorskip("onnxruntime")


@pytest.fixture
def tiny_model(tmp_path):
    """tokens (int64 [1, n]), style (float [1, 256]), speed (float [1]) -> audio = tokens / 1000 * speed."""
    from onnx import TensorProto, helper

    graph = helper.make_graph(
        [helper.make_node("Cast", ["tokens"], ["t"], to=TensorProto.FLOAT),
         helper.make_node("Div", ["t", "thousand"], ["scaled"]),
         helper.make_node("Mul", ["scaled", "speed"], ["audio"])],
        "tiny",
        [helper.make_tensor_value_info("tokens", TensorProto.INT64, [1, None]),
         helper.make_tensor_value_info("style", TensorProto.FLOAT, [1, 256]),
         helper.make_tensor_value_info("speed", TensorProto.FLOAT, [1])],
        [helper.make_tensor_value_info("audio", TensorProto.FLOAT, [1, None])],
        initializer=[helper.make_tensor("thousand", TensorProto.FLOAT, [], [1000.0])],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 8
    path = tmp_path / "tiny.onnx"
    onnx.save(model, str(path))
    return path


def _inputs(tokens, speed=1.0):
    return {"tokens": np.array([[0, *tokens, 0]], dtype=np.int64), "style": np.zeros((1, 256), np.float32),
            "speed": np.array([speed], dtype=np.float32)}


@pytest.mark.unit
def test_the_helper_runs_the_model_in_its_own_process(tiny_model):
    session = kokoro_gpu.WorkerSession(sys.executable, tiny_model, providers="cpu")
    try:
        assert session.get_providers()[0] == "CPUExecutionProvider"
        (audio,) = session.run(None, _inputs([100, 200], speed=2.0))
        assert np.allclose(audio.ravel(), [0, 0.2, 0.4, 0])
        (again,) = session.run(None, _inputs([50]))
        assert np.allclose(again.ravel(), [0, 0.05, 0]), "one helper serves every sentence"
        assert session.pid != __import__("os").getpid()
    finally:
        session.close()


@pytest.mark.unit
def test_a_model_error_is_raised_in_jarvis_and_the_helper_keeps_serving(tiny_model):
    session = kokoro_gpu.WorkerSession(sys.executable, tiny_model, providers="cpu")
    try:
        bad = _inputs([1])
        bad["style"] = np.zeros((1, 3), np.float32)
        with pytest.raises(RuntimeError):
            session.run(None, bad)
        (audio,) = session.run(None, _inputs([7]))
        assert audio.size == 3
    finally:
        session.close()


@pytest.mark.unit
def test_a_helper_that_dies_raises_instead_of_hanging(tiny_model):
    session = kokoro_gpu.WorkerSession(sys.executable, tiny_model, providers="cpu")
    session._process.kill()
    session._process.wait()
    with pytest.raises(RuntimeError):
        session.run(None, _inputs([1]))
    session.close()


@pytest.mark.unit
def test_a_helper_that_cannot_load_the_model_says_why(tmp_path):
    with pytest.raises(RuntimeError, match="load"):
        kokoro_gpu.WorkerSession(sys.executable, tmp_path / "missing.onnx", providers="cpu")


@pytest.mark.unit
def test_the_installer_builds_a_separate_environment_with_pinned_packages(tmp_path, monkeypatch):
    commands = []

    def run(cmd, **kwargs):
        commands.append([str(c) for c in cmd])
        if "venv" in cmd:
            python = kokoro_gpu.helper_python(tmp_path)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("")
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(kokoro_gpu.subprocess, "run", run)
    monkeypatch.setattr(kokoro_gpu, "_check_helper", lambda python: (True, "CUDAExecutionProvider"))
    assert kokoro_gpu.install_helper(tmp_path)
    venv, pip = commands[0], commands[1]
    assert "venv" in venv and str(tmp_path) in " ".join(venv)
    assert "--system-site-packages" not in venv, "the helper never sees Jarvis's own packages"
    assert pip[0] == str(kokoro_gpu.helper_python(tmp_path))
    for package in kokoro_gpu.HELPER_PACKAGES:
        assert "==" in package and package in pip
    assert kokoro_gpu.helper_installed(tmp_path)


@pytest.mark.unit
def test_an_install_whose_helper_cannot_use_the_card_is_reported(tmp_path, monkeypatch, capsys):
    def run(cmd, **kwargs):
        if "venv" in cmd:
            python = kokoro_gpu.helper_python(tmp_path)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("")
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(kokoro_gpu.subprocess, "run", run)
    monkeypatch.setattr(kokoro_gpu, "_check_helper", lambda python: (False, "no CUDA device"))
    assert not kokoro_gpu.install_helper(tmp_path, progress=print)
    assert not kokoro_gpu.helper_installed(tmp_path)
    assert "no CUDA device" in capsys.readouterr().out


@pytest.mark.unit
def test_kokoro_uses_the_helper_only_for_the_graphics_card_choice(monkeypatch, tiny_model):
    made = []

    class FakeWorker:
        def __init__(self, python, model, providers="cuda"):
            made.append(providers)

        def get_providers(self):
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]

        def run(self, outputs, inputs):
            return [np.full((1, 2400), 0.3, np.float32)]

    monkeypatch.setattr(kokoro_gpu, "helper_installed", lambda *a: True)
    monkeypatch.setattr(kokoro_gpu, "WorkerSession", FakeWorker)
    session, where = kokoro._create_session(tiny_model, "gpu")
    assert where == "graphics card" and made == ["cuda"]
    _session, where = kokoro._create_session(tiny_model, "cpu")
    assert where == "processor" and made == ["cuda"]


@pytest.mark.unit
def test_a_helper_that_fails_to_start_leaves_kokoro_on_the_processor(monkeypatch, tmp_path, tiny_model):
    def broken(*args, **kwargs):
        raise RuntimeError("helper could not start")

    monkeypatch.setattr(kokoro_gpu, "helper_installed", lambda *a: True)
    monkeypatch.setattr(kokoro_gpu, "WorkerSession", broken)
    _session, where = kokoro._create_session(tiny_model, "gpu")
    assert where == "processor"
