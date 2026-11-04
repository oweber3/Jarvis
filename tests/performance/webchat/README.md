# Web chat page timings

A harness for the web chat page (`src/jarvis/webchat/webchat.spec.md`): the real server and hub, the test suite's
fake daemon (`tests/fake_webchat_backend.py`) and a seeded chat store, measured in Chromium with Playwright. Run by
hand; pytest does not collect it and nothing here ships.

```bash
python tests/performance/webchat/fixture.py --long 3000          # serves the committed build on port 8766
LONG=3000 node tests/performance/webchat/measure.mjs 8766 8799 now 5
```

`--static <folder>` serves another build (for example `npx vite build --outDir <folder>` from `webchat-ui/`), and
`--repo <checkout>` another checkout's server code, so two versions can be measured one after the other. Measure on an
otherwise idle machine and alternate the versions: each fixture's hub wakes every 100 ms, so several running at once
skew the numbers. A run adds voice exchanges to the long chat, so start a fresh fixture for each run.

What it reports (medians):

| Key | Meaning |
|-----|---------|
| `firstLoad`, `firstLoadCpu4x` | `fcp`, `dcl`, `load` from the browser's timings; `ready` when the open chat's last message is on screen; JS heap and DOM nodes |
| `openLongChat` | Opening the long chat from the sidebar until its last message shows: time, heap, DOM nodes, script and layout time |
| `newMessageInLongChat` | A spoken exchange landing while the long chat is open, until it shows, and the script, layout and style time it cost |

Needs Playwright for Node with Chromium (`npm i -g playwright && npx playwright install chromium`).
