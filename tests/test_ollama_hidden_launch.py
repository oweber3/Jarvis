"""The desktop app starts ``ollama serve`` so its console helpers stay hidden.

Ollama spawns console helpers (``llama-server``, ``gpu-discover``) for GPU
discovery. They must share the server's hidden console; a helper that has to
allocate its own console opens a visible terminal window.
"""

import subprocess
import sys
import textwrap

import pytest

from desktop_app.app import _hidden_server_popen_kwargs


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows console behaviour")


def test_console_helpers_of_the_server_share_its_hidden_console():
    # The "server" spawns a console helper that reports how many processes
    # are attached to its console. A shared console includes the server too.
    helper = (
        "import ctypes; buf = (ctypes.c_uint * 16)(); "
        "print(ctypes.windll.kernel32.GetConsoleProcessList(buf, 16))"
    )
    server = textwrap.dedent(f"""
        import subprocess, sys
        out = subprocess.run([sys.executable, "-c", {helper!r}],
                             stdout=subprocess.PIPE, text=True).stdout
        print(out.strip())
    """)

    result = subprocess.run(
        [sys.executable, "-c", server],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        **_hidden_server_popen_kwargs(),
    )

    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) >= 2
