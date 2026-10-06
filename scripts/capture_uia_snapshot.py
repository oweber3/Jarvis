"""Record a UI Automation snapshot of a real window as an eval fixture.

Reads the window's accessibility tree only: it never clicks, types, focuses or moves anything. The
title and every name and value pass through Jarvis's redaction, but check the file before committing
it, because a snapshot shows what is on screen (document names, search text and the like).

Usage (from the repo root, with the app open):
    PYTHONPATH=src .mamba_env/python.exe scripts/capture_uia_snapshot.py --window notepad --name notepad_classic
    PYTHONPATH=src .mamba_env/python.exe scripts/capture_uia_snapshot.py --window "Settings" --name settings_bluetooth
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))


def main() -> int:
    parser = argparse.ArgumentParser(description='Capture a UI Automation snapshot fixture.')
    parser.add_argument('--window', required=True, help='Application name, window title or handle')
    parser.add_argument('--name', required=True, help='Fixture name, e.g. notepad_classic')
    parser.add_argument('--app', default='', help='Readable application name for the fixture')
    args = parser.parse_args()

    from jarvis.platform.windows._bounded import run_bounded
    from jarvis.platform.windows.ui_automation import snapshot
    from jarvis.tools.builtin.windows.desktop_control import _redact_data

    print(f'🔎 Reading the accessibility tree of "{args.window}"...')
    try:
        data = _redact_data(run_bounded(lambda: snapshot(args.window), 12))
    except (OSError, ValueError) as exc:
        print(f'❌ Could not capture: {exc}')
        return 1
    data['window']['hwnd'] = 1000
    target = ROOT / 'evals' / 'fixtures' / 'uia' / f'{args.name}.json'
    fixture = {'app': args.app or args.window, 'source': 'captured with scripts/capture_uia_snapshot.py',
               'snapshot': data}
    target.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'✅ Saved {len(data["elements"])} elements')
    print(f'   📄 {target.relative_to(ROOT)}')
    print('   👀 Review it for personal text before committing.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
