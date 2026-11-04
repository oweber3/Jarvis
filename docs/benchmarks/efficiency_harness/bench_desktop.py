"""Times desktop_app import + JarvisSystemTray construction offscreen, then samples idle CPU.

Never starts a daemon and never cleans up processes (both patched out).
"""
import json
import os
import sys
import time

import psutil

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
P = psutil.Process()
T0 = P.create_time()
res = {}
t = time.time()
from PyQt6.QtWidgets import QApplication  # noqa: E402
from PyQt6.QtCore import QTimer  # noqa: E402
res["pyqt_import_s"] = round(time.time() - t, 3)
t = time.time()
import desktop_app.app as app_mod  # noqa: E402
res["desktop_app_import_s"] = round(time.time() - t, 3)

app_mod.JarvisSystemTray.cleanup_orphaned_processes = lambda self: None
app_mod.JarvisSystemTray.start_daemon = lambda self, *a, **k: None

qapp = QApplication(sys.argv)
t = time.time()
tray = app_mod.JarvisSystemTray()
res["tray_construct_s"] = round(time.time() - t, 3)
show_face = "--show-face" in sys.argv
if show_face:
    tray.face_window.show()
res["since_process_start_s"] = round(time.time() - T0, 3)
res["rss_after_construct_mb"] = P.memory_info().rss // 2**20
res["threads"] = P.num_threads()

idle = float(os.environ.get("BENCH_IDLE", "60"))
c0 = sum(P.cpu_times()[:2])
w0 = time.time()
QTimer.singleShot(int(idle * 1000), qapp.quit)
qapp.exec()
res["idle_cpu_pct_one_core"] = round(100 * (sum(P.cpu_times()[:2]) - c0) / (time.time() - w0), 2)
res["rss_end_mb"] = P.memory_info().rss // 2**20
res["face_shown"] = show_face
print("DESKTOP_RESULT " + json.dumps(res), flush=True)
os._exit(0)
