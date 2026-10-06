"""Capture the real desktop widgets with isolated, non-personal demo data.

Run with the project's Python environment:
    PYTHONPATH=src python scripts/capture_readme_screenshots.py

No daemon is launched and nothing is shown on the screen: the widgets are built
offscreen from demo data (see ``ui_windows.py``) and rendered at twice the size.
Configuration, credentials and the orb state file are isolated; network access
and worker starts are blocked. Images are unretouched widget captures, not
mock-ups. Requires PyQt6 and the desktop dependencies.
"""

import os
from pathlib import Path
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_SCALE_FACTOR', '2')
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PyQt6.QtWidgets import QApplication

import ui_windows


OUTPUT = Path(__file__).resolve().parents[1] / 'docs' / 'img'

# Image file -> window in ``ui_windows.WINDOWS``.
SCREENSHOTS = {
    'face.png': 'face',
    'chat-window.png': 'chat',
    'logs.png': 'log-viewer',
    'settings-window.png': 'settings',
    'settings-mcp.png': 'settings-mcp-catalogue',
    'setup-provider.png': 'wizard-provider',
    'setup-wizard-whisper.png': 'wizard-speech',
    'setup-wizard-model.png': 'wizard-model',
    'setup-wizard-dictation.png': 'wizard-dictation',
    'setup-wizard-mcp.png': 'wizard-mcp',
    'setup-wizard-complete.png': 'wizard-complete',
    'dictation-history.png': 'dictation-history',
}


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding='utf-8', errors='replace')
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with ui_windows.isolated():
        for name, window in SCREENSHOTS.items():
            widget = ui_windows.build(window)
            if hasattr(widget, 'set_reply_mode'):
                widget.set_reply_mode('local')  # the README shows the offline baseline, without a cloud badge
            ui_windows.render(widget, OUTPUT / name)
            widget.close()
            print(f'📸 Captured {name}')


if __name__ == '__main__':
    main()
