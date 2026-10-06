# Screen awareness (`screenshot`)

Jarvis can look at the user's screen when they ask about it ("what's on my screen?", "what does this error mean?", "solve the question on my screen"). The `screenshot` tool captures the screen once, reads its text locally with offline OCR, and hands the model both the text and the image.

## Principles

- **Only when asked.** The tool runs only when the model calls it for the current request. Nothing captures the screen in the background, on a timer or ahead of a request, and no capture is ever stored: the image and the text exist in memory for that one tool result.
- **One switch turns it off.** `screen_awareness_enabled` (default `true`, Settings → Windows Control → Screen Awareness). When it is off the tool is not offered to any route (local, Codex, Claude) and a call that still arrives is refused with a message saying screen awareness is off in Settings. It takes effect when Jarvis next starts (`registry.configure_screen_tool`).
- **Local first.** Capture and OCR run on this PC with Windows' built-in OCR (`Windows.Media.Ocr`, the user's installed OCR languages, no network). The image reaches a model only as the tool result of the request that asked for it: the local chat model when it is a vision model on Ollama, or the Codex or Claude model in those reply modes. Screen awareness on means the user accepts that, in a cloud reply mode, the screen they asked about is sent to that provider.
- **Screen text is data.** The OCR text is fenced as untrusted screen content. Instructions that appear on the screen are never followed; the model reads them as part of what the user is looking at.
- **Nothing in logs.** Debug logs record only sizes, counts, timings and failure kinds: never the OCR text, the image, window titles or file names.
- **Routine and read-only.** Looking at the screen is a SAFE action and asks for no confirmation.

## Platforms

- **Windows:** the behaviour below. Registered with the Windows tools, so it also needs `windows_tools_enabled`.
- **Other platforms:** macOS keeps the interactive region capture (`screencapture -i`) with Tesseract OCR, returning text only.

## Arguments

| Argument | Values | Default |
|----------|--------|---------|
| `target` | `screen` (one monitor), `window` (the window the user is looking at), `all` (every monitor in one image) | `screen` |
| `monitor` | Optional. Which monitor for `target: screen`: a display number, `primary`, `left`, `right`, a device identifier or a name from `windows_monitor_aliases`, resolved exactly as window placement resolves monitors (`displays.resolve_monitor`) | the monitor of the window the user is looking at |

- "The window the user is looking at" is the per-request foreground window of `memory/desktop_referents.spec.md`: the foreground window unless it is Jarvis's own or the shell's, then the highest application window behind it. Typing in the chat window therefore captures the monitor (or window) behind the chat.
- With no such window, `screen` captures the primary monitor and `window` fails with a message saying no application window is in front.
- An unknown `monitor` fails with the list of connected monitors, never a guess.
- `window` captures the window's on-screen rectangle (its visible frame), so anything covering it is captured too; a minimised window cannot be captured and says so.

## Result

- `reply_text`: one header line, then the fenced OCR text.
  - Header: what was captured (monitor device and size, or the window's application and process), which window the user is looking at (application and process, never its title), and the image size the model receives.
  - The text block starts `<<<BEGIN UNTRUSTED SCREEN TEXT>>>` and ends `<<<END UNTRUSTED SCREEN TEXT>>>`, preceded by a note that it is local OCR (it may contain recognition errors) and is data, not instructions. Lines keep their reading order. The text is capped (`MAX_OCR_CHARS`) and a cut is marked.
  - No text found: the block says no text was recognised. OCR unavailable (no OCR language installed, or the runtime is missing): the header says the text could not be read and the image is still returned.
- `images`: one image of what was captured (`ToolImage`: MIME type and base64 data). It is scaled down so its longer side is at most `MAX_IMAGE_EDGE` pixels (never up) and encoded as JPEG. OCR always reads the full-resolution capture, so small text the model cannot make out in the scaled image is still in the text.
- A capture that fails (a protected desktop such as the lock screen or a UAC prompt, or a Windows error) returns a failure saying the screen could not be captured. Capture and OCR are each time-bounded.

## Delivery to the model

`ToolExecutionResult.images` carries the image beside the text. Each route delivers it with the result of that tool call only:

- **Local (Ollama):** the message carrying the tool result keeps the image internally; each chat call sends it as that message's `images` only when the model making that call reports the `vision` capability (`LLMBackend.supports_images`, read from Ollama once per model). Ollama rejects images sent to other models, so a reply that moves between the tool model and the chat model sends them only to the one that can see. Other models and the OpenAI-compatible backend receive the text only.
- **Codex:** the `jarvis_execute` result is text only and says the image follows; the image is then added to the running turn with `turn/steer`, labelled as reference data (`codex_bridge.spec.md`).
- **Claude:** an MCP `image` content block after the text block of the `jarvis_execute` result.

Images are never added to dialogue memory, the diary, the graph, the tool carry-over of the hot window, the recent-actions journal or issue reports, and a repeated call answered from the broker's stored result carries no image. The text result is carried over like any tool result (secrets scrubbed), so a follow-up such as "and the second question?" can use it without looking again.
