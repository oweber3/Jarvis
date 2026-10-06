# Local model results, 6 October 2026

The per-run records behind the local-model tables in [`docs/TOOL_MODEL_BENCHMARK.md`](../../TOOL_MODEL_BENCHMARK.md)
and the quick reference in the README. Use them to audit a result or to compare a later run case by case.

## Setup

- **Machine:** Ryzen 5 7600X3D, RTX 5070 12 GB, 32 GB RAM, Windows 11, Ollama 0.32.15.
- **Conditions:** Jarvis closed (Whisper not on the GPU), one setup at a time, nothing else using the GPU.
- **Cases:** the 28 `everyday_*`, `website_*` and `pdf_*` cases in `evals/codex_runner.py`.
- **Tools:** inert. Every call is recorded and answered from the case's scripted result, so nothing on the PC
  changes. Checks are deterministic (tool, key arguments, number of calls, kind of reply), never a judge model.

## Files

| File | What it holds |
|------|---------------|
| `engine/<setup>.json` | Full reply engine in local mode, each case run 3 times (84 requests). Models used, totals, timings, per-case pass counts and every run's calls, verdict and failure reason |
| `tool_phase.txt` | The tool-model prompt alone against Ollama at temperature 0 (`evals/tool_model_bench.py`), one line per model and its failed cases |

Engine setups (tool / chat / fast):

| File | Tool | Chat | Fast |
|------|------|------|------|
| `gemma4-12b.json` | `gemma4:12b` | `gemma4:12b` | `gemma4:12b` |
| `gpt-oss-20b.json` | `gpt-oss:20b` | `gpt-oss:20b` | `gpt-oss:20b` |
| `gpt-oss-20b-q4km.json` | unsloth `gpt-oss-20b` Q4_K_M | same | same |
| `granite4.2-8b.json` | `granite4.2:8b` | `granite4.2:8b` | `granite4.2:8b` |
| `qwen3.5-9b-tool.json` | `qwen3.5:9b` | `qwen3.5:9b` | `qwen3.5:4b` |
| `qwen3.5-9b-no-tool.json` | none | `qwen3.5:9b` | `qwen3.5:4b` |
| `qwen3.5-4b.json` | `qwen3.5:4b` | `qwen3.5:4b` | `qwen3.5:4b` |

Reply text is left out because replies can mention the local time and time zone. Each run keeps
`reply_kind` (reply, question or awaiting confirmation). `web_search_replies_with_invented_results` counts
the `everyday_web_search` runs whose reply named race results: the scripted search result contains none,
so any winner or driver was made up. The checks do not score this.

## Reproducing

From the repository root, with Jarvis closed:

```bash
set PYTHONPATH=src;evals
python evals/codex_runner.py --mode local --only everyday website pdf --repeat 3 --tool-model gemma4:12b --chat-model gemma4:12b --fast-model gemma4:12b --out gemma4-12b.json
python evals/tool_model_bench.py gemma4:12b,qwen3.5:9b,gpt-oss:20b
```

`--tool-model ""` runs without a tool model. The runner's `--out` file includes reply text; check it before
committing it.
