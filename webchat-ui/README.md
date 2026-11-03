# Jarvis web chat page and phone app

The source of the page Jarvis serves for its web chat (`src/jarvis/webchat/webchat.spec.md`) and of the phone
app it serves to paired phones (`src/jarvis/remote/remote.spec.md`). One React project, built on
[assistant-ui](https://www.assistant-ui.com/) (MIT licence), makes both: the phone app shares every chat component
and adds a pairing screen, the particle-sphere orb, confirmations, quick actions and voice (`src/phone/`).
The **build outputs are committed** to `src/jarvis/webchat/static/` (web chat) and `src/jarvis/remote/static/`
(phone app), so running Jarvis, and its Python tests, never need Node. Node is only for changing the pages.

## Build

```bash
python -m desktop_app.web_chat_theme   # only when themes.py or orb_widget.py changed (theme.css, orbPalette.ts)
cd webchat-ui
npm ci
npm run build                          # type-checks, then writes both builds
npm run build:web                      # only ../src/jarvis/webchat/static/
npm run build:phone                    # only ../src/jarvis/remote/static/ (vite build --mode phone, from phone.html)
npm test                               # the unit tests of both apps
```

Commit the rebuilt `static/` folders with the source change. `tests/test_webchat_bundle.py` fails if the build
names an outside host, a hosted service or any tracking, and `tests/test_webchat_theme.py` fails if
`src/generated/theme.css` is out of date.

## Develop

Turn on `web_chat_enabled` in Jarvis, start it, then:

```bash
cd webchat-ui
npm run dev                            # http://localhost:5173, /api proxied to Jarvis on port 8766
```

Use `JARVIS_WEB_CHAT_URL` to point the proxy at another port. For the phone app, `vite --mode phone` proxies to
phone access (port 8765 by default); pair it from the dev page as a phone would.

The phone app's home-screen icons are drawn by `PYTHONPATH=src python webchat-ui/tools/make_phone_icons.py`.

## Where the code comes from

The chat components are **assistant-ui's own source, copied into this project and then edited**, not an
installed package:

| | |
|---|---|
| Copied with | `assistant-ui` CLI **0.0.121** (`npx assistant-ui@0.0.121 add thread thread-list markdown-text model-selector`) |
| Runtime library | `@assistant-ui/react` **0.15.25**, `@assistant-ui/react-markdown` **0.14.19** |
| Pristine copy | git commit `a1b2540` (`chore(webchat-ui): add the project and assistant-ui components exactly as the CLI copied them`) |

`git diff a1b2540 -- webchat-ui/src/components` is everything changed locally. To pull upstream improvements, run
the same `add` command in an empty scratch project at the new CLI version, diff its files against the pristine
copy, and apply the differences to ours.

| File | Origin |
|---|---|
| `components/thread.aui.tsx` | assistant-ui, trimmed (no tool and reasoning groups, feedback, editing, branching, dictation), with a notice line, a slot for the model picker, a slot above the composer (the phone app's dock), message times, day dividers, scroll-to-search-result, and the attachment chips, drop target and buttons wired in |
| `components/attachment.aui.tsx` | assistant-ui's attachment components (commit `a1b2540`), trimmed to a chip with a thumbnail or icon (no preview dialog or avatar), plus the Screenshot button and the send gate notice |
| `components/thread-list.aui.tsx` | assistant-ui, with "Pin" and "Move to project" items instead of Archive |
| `components/markdown-text.tsx`, `model-selector.tsx`, `model-selector.aui.tsx`, `tooltip-icon-button.tsx`, `hooks/use-copy-to-clipboard.ts` | assistant-ui, unchanged |
| `components/ui/*` | shadcn primitives from the same registry; `cn` imported from `@/lib/utils` |
| `attachments.ts`, `attachmentAdapter.ts` | written for Jarvis: the attachment rules free of React (labels, sizes, what blocks a send and why, the Screenshot marker file) and the adapter that connects the composer to the server's upload endpoint; the page never reads a file's name or path to the server |
| `api.ts`, `conversion.ts`, `steps.ts`, `components/ToolSteps.tsx`, `sync.ts`, `confirm.ts`, `layout.ts`, `models.ts`, `theme.ts`, `time.ts`, `sidebar.ts`, `search.ts`, `shortcuts.ts`, `scrollTarget.ts`, `hooks/use-message-search.ts`, `useJarvis.tsx`, `App.tsx`, `components/ModelPicker.tsx`, `components/ProjectsSidebar.tsx`, `components/ConfirmDialog.tsx`, `components/MessageTime.tsx`, `startScreen.ts`, `components/StartScreen.tsx` | written for Jarvis: the API client, the poll and ordering logic (`sync.ts`, free of React so it can be tested), the runtime wiring, the model and effort pickers (the model menu is assistant-ui's `ModelSelector`; the effort menu is ours), the light and dark switch, the projects sidebar, the delete confirmations and the start screen (greeting and suggestion cards) |
| `mic.ts`, `components/MicButton.tsx` | written for Jarvis: the microphone button. `mic.ts` (free of React so it can be tested) drives the daemon's recording through `/api/mic/*`; the page never uses the browser's microphone or speech APIs |
| `src/phone/` | written for Jarvis, **phone app only** (nothing shared with the web chat imports it): `main.tsx`, `PhoneApp.tsx` (pairing gate), `PhoneProvider.tsx` (the poll's `phone` part, the owner's switch, the voice session and voice mode), `PhoneShell.tsx` and its pieces (`ChatsDrawer`, `ConnectionStatus`, `PhoneSettings`, `Sheet`, `QuickActions`, `PhoneMicButton`, `PhoneDock`, `ConfirmCard`, `PhoneToast`, `VoiceModeDialog` and `VoiceModeScreen`, `PairScreen`); `voiceMode.ts` (opening and closing voice mode, its captions and the latest exchange, free of React), `confirmation.ts`; `orbModel.ts` and `Orb.tsx` (a port of the desktop orb, checked against it by `orbModel.golden.json`), `orbState.ts`; `pcm.ts` (16 kHz PCM16 and the end-of-speech detector), `micCapture.ts` and `public-phone/pcm-worklet.js` (the AudioWorklet), `audioPlayer.ts` (reply audio through an analyser), `voiceSession.ts` (the order of a spoken exchange, free of React and tested with fake ports), `phoneApi.ts`, `storage.ts`, `keyboard.ts`, `countdown.ts`, `statusLine.ts`. It records with the phone's own microphone only; the web chat's bundle contains no browser audio API (`tests/test_webchat_mic_page.py`) |
| `generated/orbPalette.ts` | generated by `python -m desktop_app.web_chat_theme` from `ORB_PALETTE` and the desktop orb's state table, so the phone's orb colours and looks cannot drift |
| `index.css` | maps assistant-ui's colour tokens onto the Jarvis HUD palettes, dark and light (`generated/theme.css`) |

## Offline

Nothing here calls a CDN, analytics or assistant-ui Cloud. The pages talk only to their own origin
(`/api/...`), fonts are the system's, icons are bundled (`lucide-react`), and `@assistant-ui/react`'s optional
cloud client is never imported. Keep it that way: add no URL and no cloud import to this folder.
