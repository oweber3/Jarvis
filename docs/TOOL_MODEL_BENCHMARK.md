# Tool-use measurements: Codex, local models and the tool model

Measured on 5 and 6 October 2026 on a desktop PC (Ryzen 5 7600X3D, RTX 5070 12 GB, 32 GB RAM, Windows 11,
Ollama 0.32.15). Jarvis was not running, so Whisper did not share the GPU; with it loaded, models that do not
fit in VRAM spill further onto the CPU. Codex ran `gpt-6-luna` at low effort.

All tool runs are inert: the eval runner (`evals/codex_runner.py`) records each call and answers it from a
scripted result, so nothing on the PC changes. Checks are deterministic (which tool, which key arguments, how
many calls, whether the reply is an answer or a question), never a judge model. "Correct" below means the
case's check passed. Once the right calls are made, a reply that closes with a follow-up question ("Anything
else?") still counts as a confirmation. The Codex section was scored on 5 October with a stricter rule that
counted any reply containing `?` as a question, so its figures may be a little low.

## Case sets

| Set | Cases | What it covers |
|-----|-------|----------------|
| `everyday_*` | 23 | Volume, media, system info, Settings pages, brightness, audio output, files and folders, windows, desktops, hotkeys, websites, weather, web search, UI clicks. One obvious tool each |
| `website_*` | 2 | "Open YouTube in my browser on the right side", "Open YouTube." |
| `pdf_*` | 3 | Go to page 42, jump to a chapter, page request with no PDF open |
| others | 25 | Earlier Codex cases: placement, workspaces, safety, injection, languages, follow-ups |

The local sweeps use the 28 cases of the first three sets.

## Codex reply mode

| Measurement | Result |
|-------------|--------|
| Website cases before `openWebsite` (3 runs each) | 0/6. One run reproduced a reported bug (empty Chrome placed right, YouTube opened unplaced through DevTools `new_page`); the rest opened YouTube unplaced |
| Website cases after `openWebsite` (5 runs each) | 10/10, one `openWebsite` call each; "right side" 4.5 to 7.5 s against 7.5 to 10.7 s before |
| PDF and website cases with the 30 real DevTools tools offered (3 runs) | 15/15, no DevTools call |
| Everyday sweep, DevTools offered / not offered (3 runs each) | 63/69 / 65/69; median 6.8 s / 6.0 s; p90 29.2 s / 11.7 s. With DevTools, "reopen the tab I just closed" called `chrome-devtools__list_pages` first in 3/3 |
| "Delete report.pdf from my documents folder" (4 runs) | Before the `openPath` description change: `openPath` on the file first in 4/4 (would open the PDF). After: `localFiles` alone, stopping at the confirmation, 4/4 |
| Full suite, all cases (2 runs) | 98/106, median 6.1 s, p90 10.2 s |

The DevTools MCP adds about 24,000 characters of tool definitions to every Codex request (Jarvis's builtins are
about 15,000) and drives its own debugging Chrome. It was left out of the configuration for the later runs.

## Local models inside Jarvis

Full reply engine in local mode, the 28 cases run three times each (84 requests per setup), one setup at a
time, on 6 October. "Made up" counts the three web-search runs whose reply named race results: the scripted
search result contains none, so any driver or winner was invented. The checks do not score this. Every run's
calls, verdict and timing are in [`benchmarks/local-models-2026-10-06/`](benchmarks/local-models-2026-10-06/README.md).

| Setup (tool / chat / fast) | Download | Correct | Median | p90 | First tool | Made up |
|----------------------------|----------|---------|--------|-----|------------|---------|
| **`gemma4:12b` for all three** | 7.6 GB | **82/84** | **2.7 s** | 3.6 s | 1.1 s | 2/3 |
| `gpt-oss:20b` for all three | 13.8 GB | 74/84 | 5.9 s | 15.1 s | 1.2 s | 2/3 |
| `granite4.2:8b` for all three | 5.3 GB | 70/84 | 1.5 s | 4.5 s | 0.7 s | 2/3 |
| unsloth `gpt-oss-20b` Q4_K_M for all three | 11.6 GB | 69/84 | 3.2 s | 9.8 s | 0.8 s | 2/3 |
| `qwen3.5:9b` / `qwen3.5:9b` / `qwen3.5:4b` | 6.6 + 3.3 GB | 64/84 | 1.7 s | 2.6 s | 0.8 s | 0/3 |
| `qwen3.5:4b` for all three | 3.3 GB | 63/84 | 1.4 s | 2.7 s | 0.6 s | 1/3 |
| none / `qwen3.5:9b` / `qwen3.5:4b` | 6.6 + 3.3 GB | 53/84 | 2.5 s | 3.9 s | — | 0/3 |

- **`gemma4:12b`** missed one file search (looked the file up before opening it) and one "no PDF open" case
  (opened a file instead). It fits a 12 GB card; it was not measured beside Whisper.
- **`gpt-oss:20b`** searched the web twice for one question in every run, named a monitor where "right side"
  means a zone, and twice went looking for the PDF file instead of turning the open one's page. It spills out
  of 12 GB, so the first request after loading is slow.
- **`granite4.2:8b`** opened YouTube with `appControl` instead of `openWebsite`, ran a web search for "search
  YouTube", and took four to six calls (focusing windows, reading the UI) before sending the reopen-tab hotkey.
- **Qwen 3.5** handles media, volume, settings and system info reliably but fails window placement
  (minimise, left side, zones) and searches for files instead of opening them. Setting the tool model to
  `qwen3.5:9b`, the same model as chat, lifts the setup from 53 to 64 at no extra memory. Without a tool model
  it often answers without calling the tool, and says it turned to page 42 when nothing was called. Two of
  the tool-model setup's misses were honest "no PDF is open" replies worded in a way the check does not
  recognise.

Single runs from 5 October that were not repeated (scored with the stricter `?` rule):

| Setup (tool / chat / fast) | Correct | Median | First tool | Notes |
|----------------------------|---------|--------|------------|-------|
| none / `gpt-oss:20b` / `qwen3.5:4b` | 41/56 (2 runs) | 34 s | 31 s | Two models swap in and out of VRAM on every step |
| `gpt-oss:20b` / `qwen3.5:9b` / `qwen3.5:4b` | 20/28 | 25.4 s | 16.0 s | Models reload every request |
| `granite4.2:8b` for all three, reasoning on | 22/28 | 13.4 s | 3.5 s | Reasoning slows the reply phase too |
| `granite4.2:8b-q6_K` for all three | 18/28 | 2.4 s | 0.8 s | No gain over Q4_K_M |
| LoopTool-8B (Q4_K_M GGUF) for all three | 19/28 | 5.2 s | 1.8 s | Reasons even with reasoning off; reasoning leaked into one reply |

## Tool-model candidates (tool phase alone)

`evals/tool_model_bench.py`: the tool-model prompt, the whole builtin catalogue, 8,192-token context,
temperature 0, so runs repeat exactly. Measured on 6 October unless marked.

| Model | Size | Correct | First tool |
|-------|------|---------|------------|
| `gemma4:12b` | 7.6 GB | **28/28** | 0.5 s |
| `qwen3.5:9b` | 6.6 GB | 25/28 | 0.7 s |
| `gpt-oss:20b` (MXFP4) | 13.8 GB | 23/28 | 1.4 s |
| `qwen3:4b` | 2.5 GB | 23/28 | 2.5 s |
| `qwen3.6:35b` (MoE) | 23.9 GB | 23/28 | 1.4 s |
| `qwen3.5:4b` | 3.3 GB | 22/28 | 0.4 s |
| `qwen3.5:9b`, reasoning on | 6.6 GB | 22/28 | 1.3 s |
| `granite4.2:8b`, reasoning on | 5.3 GB | 21/28 | 2.9 s |
| unsloth `gpt-oss-20b` Q4_K_M | 11.6 GB | 20/28 | 0.9 s |
| LoopTool-8B Q4_K_M, reasoning on (5 October) | 5.0 GB | 20/28 | 1.9 s |
| LoopTool-8B Q4_K_M (5 October) | 5.0 GB | 17/28 | 1.9 s |
| `granite4.2:8b` | 5.3 GB | 16/28 | 0.5 s |
| `granite4.2:8b-q6_K` (5 October) | 7.2 GB | 16/28 | 0.7 s |
| `ministral-3:14b` | 9.1 GB | 14/28 | 0.3 s |
| `dolphin3:8b` (5 October) | 4.9 GB | 0/28 | — (no tool support) |

Adding a user's warm profile (stored profile and directives) to the tool prompt lowered `gpt-oss:20b` from 24
to 19 of 28 on 5 October, with the model asking instead of acting, so the tool phase leaves it out.

## Findings

- **`gemma4:12b` is the best local all-rounder measured.** It was the only model to pass every case in the
  tool phase and scored highest through the full engine, at under 3 seconds and about 7.6 GB.
- **Neither public benchmarks nor the tool phase alone predict the full engine.** On BFCL v4, Granite 4.2 8B
  (0.524) and `qwen3.5:4b` (0.503) are close. In the tool phase `qwen3.5:4b` beat Granite 22 to 16, yet
  through the full engine Granite scored 70 of 84 against 63. Measure a candidate inside Jarvis before
  recommending it.
- **Use the chat model as the tool model.** With `qwen3.5:9b` in both roles the full engine went from 53 to 64
  of 84 with no extra memory. A different, larger tool model only pays off if both stay loaded together.
- **Mixture-of-experts models suit a 12 GB GPU.** `gpt-oss:20b` uses about 3.6B of its 21B parameters per
  token, so the part that spills into system RAM costs little. A dense 30B model at Q4 (about 18 GB) would
  read all of its weights every token.
- **Compression trades accuracy for speed on `gpt-oss`.** Its expert layers are already 4-bit (MXFP4). The
  Q4_K_M build is 2.2 GB smaller and roughly twice as fast through the engine (3.2 s against 5.9 s median) but
  scored 69 against 74.
- **Thin search results invite invention.** When the web search returned a title and no content, every
  setup except the `qwen3.5:9b` ones named a winner and podium in at least one of three runs. The checks do
  not score this, so the tables count it separately.
- **Context size is a trap.** Granite's default context is 4,096 tokens and the tool catalogue alone is about
  4,700, so Ollama rejected every request until the benchmark sent Jarvis's 8,192. Any tool model needs
  `llm_num_ctx` of at least 8,192.
- **Recurring mistakes** across small models: a monitor instead of a zone ("right side"), "open" instead of
  "focus", a web search instead of opening the site's own search, a missing `desktop` argument for "next
  desktop", and extra look-up calls before acting.

## Checks on a real desktop

Read-only or self-contained, with only test windows closed afterwards:

- `pdfNavigate` in Chrome: `goto 12` and a `find` for a chapter topic (outline match, page 14) landed on the right page in
  under a second, confirmed by screenshot. Edge's page box changes its number but not the view without Enter.
- `openPath` with a zone: a PDF opened in a new Chrome window and a folder in a new Explorer window, each
  placed in its zone in under a second. Notepad merged the test file into an existing window as a tab.
- `uiControl` on Task Manager and PowerToys Settings (both elevated): refused with the administrator message;
  Chrome still read 55 elements.
- Every tool's read-only actions ran successfully, each in under 1.3 s.
- Tool-model mode end to end with real tools (`gpt-oss:20b` everywhere): "What's using the most RAM?" and
  "What time is it in Tokyo?" (4.8 s warm) answered correctly.

## Reproducing

```bash
set PYTHONPATH=src;evals
python evals/codex_runner.py --mode codex --only everyday website pdf --repeat 3
python evals/codex_runner.py --mode local --only everyday website pdf --repeat 3 --tool-model gemma4:12b --chat-model gemma4:12b --fast-model gemma4:12b
python evals/tool_model_bench.py gemma4:12b,qwen3.5:9b,granite4.2:8b@think
```

`EVAL_DEVTOOLS=0` leaves the DevTools tools out of Codex runs. Run one setup at a time with Jarvis closed: two
models sharing the GPU more than doubled the median time and caused timeouts.

Sources for the models considered: [Ollama tool-calling models](https://localaimaster.com/blog/best-ollama-models-tool-calling),
[Granite 4.2 review](https://www.eesel.ai/blog/granite-4-2-review), [BFCL v4](https://llm-stats.com/benchmarks/bfcl-v4),
[LoopTool-8B](https://huggingface.co/zhangkangning/LoopTool-8B), [unsloth gpt-oss-20b GGUF](https://huggingface.co/unsloth/gpt-oss-20b-GGUF).
D-CORE-8B was considered but has no published weights.
