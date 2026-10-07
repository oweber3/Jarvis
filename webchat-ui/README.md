# Jarvis web chat page

The source of the page Jarvis serves for its web chat (`src/jarvis/webchat/webchat.spec.md`). It is a React
app built on [assistant-ui](https://www.assistant-ui.com/) (MIT licence). The **build output is committed** to
`src/jarvis/webchat/static/`, so running Jarvis, and its Python tests, never need Node. Node is only for
changing the page.

## Build

```bash
python -m desktop_app.web_chat_theme   # only when src/desktop_app/themes.py changed
cd webchat-ui
npm ci
npm run build                          # type-checks, then writes ../src/jarvis/webchat/static/
npm test                               # the page's unit tests
```

Commit the rebuilt `static/` folder with the source change. `tests/test_webchat_bundle.py` fails if the build
names an outside host, a hosted service or any tracking, and `tests/test_webchat_theme.py` fails if
`src/generated/theme.css` is out of date.

## Develop

Turn on `web_chat_enabled` in Jarvis, start it, then:

```bash
cd webchat-ui
npm run dev                            # http://localhost:5173, /api proxied to Jarvis on port 8766
```

Use `JARVIS_WEB_CHAT_URL` to point the proxy at another port.

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
| `components/thread.aui.tsx` | assistant-ui, trimmed (no attachments, tool and reasoning groups, feedback, editing, branching, dictation), with a notice line and a slot for the model picker |
| `components/thread-list.aui.tsx` | assistant-ui, with a "Move to project" item instead of Archive |
| `components/markdown-text.tsx`, `model-selector.tsx`, `model-selector.aui.tsx`, `tooltip-icon-button.tsx`, `hooks/use-copy-to-clipboard.ts` | assistant-ui, unchanged |
| `components/ui/*` | shadcn primitives from the same registry; `cn` imported from `@/lib/utils` |
| `api.ts`, `conversion.ts`, `models.ts`, `theme.ts`, `useJarvis.tsx`, `App.tsx`, `components/ModelPicker.tsx`, `components/ProjectsSidebar.tsx` | written for Jarvis: the API client, the runtime wiring, the model and effort pickers (the model menu is assistant-ui's `ModelSelector`; the effort menu is ours), the light and dark switch and the projects sidebar |
| `index.css` | maps assistant-ui's colour tokens onto the Jarvis HUD palettes, dark and light (`generated/theme.css`) |

## Offline

Nothing here calls a CDN, analytics or assistant-ui Cloud. The page talks only to its own origin
(`/api/...`), fonts are the system's, icons are bundled (`lucide-react`), and `@assistant-ui/react`'s optional
cloud client is never imported. Keep it that way: add no URL and no cloud import to this folder.
