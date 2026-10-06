# Tool-use measurements: Codex, local models and the tool model

Measured on 5 October 2026 on a desktop PC (Ryzen 5 7600X3D, RTX 5070 12 GB, 32 GB RAM, Windows 11,
Ollama 0.32.15). Jarvis was not running, so Whisper did not share the GPU; with it loaded, models that do not
fit in VRAM spill further onto the CPU. Codex ran `gpt-6-luna` at low effort.

All tool runs are inert: the eval runner (`evals/codex_runner.py`) records each call and answers it from a
scripted result, so nothing on the PC changes. Checks are deterministic (which tool, which key arguments, how
many calls, whether the reply is an answer or a question), never a judge model. "Correct" below means the
case's check passed. A check counts any reply containing `?` as a question, which penalises personas that end
with a rhetorical question even after a correct tool call; the per-setup notes say where that mattered.

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

Full reply engine, local mode, the 28 cases, one run each unless stated.

| Setup (tool / chat / fast) | Correct | Median | First tool | Notes |
|----------------------------|---------|--------|------------|-------|
| none / `qwen3.5:9b` / `qwen3.5:4b` (a typical mixed setup) | 28/56 (2 runs) | 2.4 s | 1.8 s | Often answers without calling the tool, sometimes narrating the action ("the volume has been silenced") |
| none / `gpt-oss:20b` / `qwen3.5:4b` | 41/56 (2 runs) | 34 s | 31 s | Two models swap in and out of VRAM on every step |
| none / `gpt-oss:20b` / `gpt-oss:20b` | 22/28 | 10.9 s | 7.7 s | Router, planner and the 13,400-character persona prompt all on one partly offloaded model |
| `gpt-oss:20b` / `qwen3.5:9b` / `qwen3.5:4b` | 20/28 | 25.4 s | 16.0 s | Tool choices right in 25/28 (5 failures were replies containing `?`); models reload every request |
| **`gpt-oss:20b` for all three** | **25/28** | **7.0 s** | **1.9 s** | Recommended setup to try |
| `granite4.2:8b` (Q4_K_M) for all three, reasoning off | 19/28 | 2.1 s | 0.7 s | Fits beside Whisper on the GPU |
| `granite4.2:8b` for all three, reasoning on | 22/28 | 13.4 s | 3.5 s | Reasoning slows the reply phase too |
| `granite4.2:8b-q6_K` for all three | 18/28 | 2.4 s | 0.8 s | No gain over Q4_K_M |
| LoopTool-8B (Q4_K_M GGUF) for all three | 19/28 | 5.2 s | 1.8 s | Reasons even with reasoning off; reasoning leaked into one reply |
| unsloth `gpt-oss-20b` Q4_K_M (11.6 GB) for all three | 22/28 | 4.6 s | 1.4 s | 10.2 of 11.9 GB on the GPU; single run |

Two prompt-level fixes for `qwen3.5:9b` were tried and reverted because neither moved the result beyond noise:
a reminder when the model answers in words while the plan needs a tool (52%), and a "never claim an action
without its tool result" rule (50%). The model ignored the reminder and repeated the claim.

## Tool-model candidates (tool phase alone)

`evals/tool_model_bench.py`: the tool-model prompt, the whole builtin catalogue, 8,192-token context,
temperature 0, so runs repeat exactly.

| Model | Size | Correct | First tool |
|-------|------|---------|------------|
| `gpt-oss:20b` (MXFP4) | 13.8 GB | **24/28** | 1.4 s |
| unsloth `gpt-oss-20b` Q4_K_M | 11.6 GB | 23/28 | 1.0 s |
| `granite4.2:8b`, reasoning on | 5.3 GB | 21/28 | 2.5 s |
| LoopTool-8B Q4_K_M, reasoning on | 5.0 GB | 20/28 | 1.9 s |
| `granite4.2:8b` | 5.3 GB | 17/28 | 0.5 s |
| LoopTool-8B Q4_K_M | 5.0 GB | 17/28 | 1.9 s |
| `granite4.2:8b-q6_K` | 7.2 GB | 16/28 | 0.7 s |
| `qwen3.6:35b` (MoE) | 23.9 GB | 12/28 | 1.6 s |
| `ministral-3:14b` | 9.1 GB | 11/28 | 0.3 s |
| `qwen3.5:4b` | 3.3 GB | 10/28 | 0.5 s |
| `qwen3.5:9b` (also with reasoning) | 6.6 GB | 9/28 | 0.7 s |
| `qwen3:4b` | 2.5 GB | 7/28 | 3.0 s |
| `gemma4:12b` | 7.6 GB | 1/28 | — |
| `dolphin3:8b` | 4.9 GB | 0/28 | — (no tool support) |

The Qwen 3.5 rows were measured with a shorter, earlier prompt and Ollama's default context; every other row
uses the shipped prompt at 8,192 tokens. Adding a user's warm profile (stored profile and directives) to the
tool prompt lowered `gpt-oss:20b` from 24 to 19 of 28, with the model asking instead of acting, so the tool
phase leaves it out.

## Findings

- **Public benchmarks did not predict these results.** On BFCL v4, Granite 4.2 8B (0.524) and `qwen3.5:4b`
  (0.503) are close; here they scored 17 and 10 of 28.
- **Mixture-of-experts models suit a 12 GB GPU.** `gpt-oss:20b` uses about 3.6B of its 21B parameters per
  token, so the part that spills into system RAM costs little. A dense 30B model at Q4 (about 18 GB) would
  read all of its weights every token.
- **Compression helps little on `gpt-oss`.** Its expert layers are already 4-bit (MXFP4), so the smallest
  community build saves about 2.3 GB; Q4_K_M saves 2.2 GB and was a third faster in this run.
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
python evals/codex_runner.py --mode local --only everyday website pdf --tool-model gpt-oss:20b --chat-model gpt-oss:20b --fast-model gpt-oss:20b
python evals/tool_model_bench.py gpt-oss:20b,granite4.2:8b,granite4.2:8b@think
```

`EVAL_DEVTOOLS=0` leaves the DevTools tools out of Codex runs.

Sources for the models considered: [Ollama tool-calling models](https://localaimaster.com/blog/best-ollama-models-tool-calling),
[Granite 4.2 review](https://www.eesel.ai/blog/granite-4-2-review), [BFCL v4](https://llm-stats.com/benchmarks/bfcl-v4),
[LoopTool-8B](https://huggingface.co/zhangkangning/LoopTool-8B), [unsloth gpt-oss-20b GGUF](https://huggingface.co/unsloth/gpt-oss-20b-GGUF).
D-CORE-8B was considered but has no published weights.
