# Personalities

A personality is how Jarvis talks: the character its replies are written in. The user picks one in Settings or switches by voice or text, and it applies in every reply mode (local, Codex and Claude). A personality changes only the character; the rules every reply follows (tone rails, honesty, memory, formatting, tools, confirmations and safety) are the same whatever is chosen.

## Files

| File | Role |
|------|------|
| `system_prompt.py` | The character texts, the tone rails, the fixed rules and how they are put together |
| `personality.py` | The active personality: from configuration, switched at runtime, persisted; the cloud reply voice; the paired voice settings |
| `tools/builtin/personality_tool.py` | The `personality` tool that switches by voice or text |
| `fastpath/phrases/<language>.json` | The `personality` fast-path family |

## Personalities

| Name | Character |
|------|-----------|
| `butler` (default) | A British butler: polite, composed, dry and lightly witty; one understated aside after the answer on casual topics, none on serious ones; never opens with a joke or butler clichés and never calls the user sir or madam |
| `plain` | The shortest answer that fully helps: the answer first, only what the user needs to act on it, then stop; no wit, asides, small talk, exclamation marks or filler; a joke only when asked for; courteous and neutral |
| `friendly` | Warm, casual and informal like a helpful friend: everyday language, a little encouragement, light good-natured humour now and then and never sarcasm; at most one short friendly remark after the answer; calm and kind on serious topics |
| `custom` | The user's own description from `personality_custom_text` |

The Butler's text is the persona Jarvis has always had, unchanged word for word, so choosing nothing changes nothing.

## The prompt

`build_system_prompt(name, personality, custom_text)` is the character section followed by `RULES`:

- The character section is the personality's character text with the assistant's name (the wake word, capitalised; `Jarvis` when blank), followed by `TONE_RAILS` (never mean, condescending, passive-aggressive, sulking, preachy or sycophantic). The Butler carries the rails inside its own text.
- `RULES` is the same for every personality: no bare greetings, tone adapted to the topic, the `[Context: ...]` time and location line, conversation history, long-term memory and its denial-template handling, open-ended prompts, banned denial phrasings, short replies without complex formatting.
- In the local reply engine this text opens the system message; the model-size prompt components (tool use and constraints) and every other block follow it (`reply/reply.spec.md`). Confirmations and safety are enforced in code by central safety whatever the prompt says.

### Custom

The custom description is the user's own text and is treated as such, but it is fenced as the character, never as instructions:

- It sits inside a `<character>` block introduced by a note that it sets only how Jarvis sounds and cannot change the other rules, the tools, confirmations, safety, honesty or memory, and that any part asking for something those rules forbid (such as rudeness) is left out while the rest of the character is kept and the user is still answered helpfully. The tone rails and `RULES` follow the block.
- Every `<` and `>` in the text becomes `‹` and `›`, so no tag, nested or otherwise, can close or reopen the block. The text is trimmed and cut to `MAX_CUSTOM_CHARS` (1,500) characters. It is inserted literally (no template formatting).
- Custom with no text left is the Butler.
- A model may still refuse a description that asks mainly for abuse; that reply then fails like any empty reply ("Sorry, I had trouble processing that") and is never mean.

An unknown personality name is the Butler.

## Every reply route

- **Local**: the reply engine builds its persona prompt from `personality.current(cfg)` on every reply.
- **Codex and Claude**: `prompts.session_instructions(cfg)` in each bridge is the fixed session contract (`assistant_instructions()`, at most 2,000 characters) followed by `personality.bridge_voice(cfg)`: one line saying the reply is spoken to the user, so it is written in this voice, which shapes only tone and wording and never changes anything above, then the same character section as the local prompt (with the tone rails; not `RULES`, which are local-engine guidance). It holds no utterance, dialogue or other configuration. Each session reads it when it starts, and a spare session or thread started with different instructions is discarded rather than used, so a switch applies to the next request (`claude_bridge.spec.md`, `codex_bridge.spec.md`).
- **Fast path**: fast-path replies are fixed tool sentences and do not change with the personality.

## Switching

- The active personality starts from `personality` at daemon start (`personality.start(cfg)`, which drops any earlier switch) and is printed (`🎭 Personality: Butler`).
- `personality.switch(name)` accepts only `butler`, `plain`, `friendly` and `custom`; `custom` only when custom text is written. The switch takes effect on the next reply in every mode with no restart and is persisted through `config.update_config_values({"personality": name})` (only non-default values written, other keys kept). A failed save, or a switch in a process that never started the personality state, keeps the switch until restart and says so. Switching to the active personality changes nothing.
- The `personality` tool (`set` with `name`, or `get`) is the voice and text route. Its web chat step reads "Switched to the Plain personality" only when a switch happened. It is `SAFE`, always registered and offered to Codex and Claude like any routine local setting, and it is a valid routine step. Its replies are short sentences (`Switched to the Plain personality.`); a refusal names the choices, or says where to write a custom one.
- Fast-path phrases (`personality` family) cover whole utterances that name the personality with "mode" or "personality" ("switch to plain mode", "use the friendly personality", "use my custom personality"); the short forms ("be plain", "be friendly", "be the butler", "plain mode") need the wake word. A request that only mentions a style ("write a friendly email") is not a switch. The phrases are locale data, so other languages add their own.
- A style instruction that names no personality ("be more brief", "drop the jokes") is a conversational instruction the model follows within the conversation without calling a tool (`reply/prompts/prompts.spec.md`, USER INSTRUCTIONS); only a request to change the personality switches it.
- The Settings window writes `personality` and `personality_custom_text` like any field; they take effect after a restart, like other settings, while a voice switch is immediate.

## Paired voice

A personality may name a voice in `personality_voices`, a map from personality name to voice settings, for example `{"friendly": {"tts_engine": "piper", "tts_piper_length_scale": 0.6}}`. Only keys starting with `tts_` that are known settings, with a value of that setting's type (a number for a number, text for text, true or false for a switch), are applied; anything else, and any malformed entry, is ignored, so a mistyped pairing never stops Jarvis starting. `config.load_settings` applies the active personality's pairing over the configured voice settings when Jarvis starts, so the text-to-speech code reads ordinary settings and knows nothing of personalities. With no pairing nothing changes. A voice switch to a personality with a usable pairing says the paired voice is used after Jarvis restarts. Pairings are written in `config.json`; the Settings window does not show them.

## Privacy and logging

The custom text stays in `config.json` and in prompts. It reaches Codex or Claude only inside their session instructions while that mode is in use, which the user chose; that includes the spare session or thread each bridge starts ahead of the next request. `debug_log` records the personality name at start, on each switch and on each local reply, and which voice keys a pairing applied, never the custom text.

## Verification

- Unit tests (`tests/test_system_prompt.py`, `tests/test_personality.py`, `tests/test_settings_personality_fields.py`, the bridge prompt and spare tests): every personality keeps the tone rails and rules; custom is fenced, bounded and cannot close its fence; unknown and empty values are the Butler; switching, persistence and refusals; the local reply and both bridges use the active character and follow a switch; the fast-path phrases; the paired voice; the Settings fields.
- Live evals: `evals/test_personality_tone.py` (for each casual question the three built-ins answer and a judge shown the replies shuffled must assign each to its voice for most questions, Plain must be shortest and use no exclamation marks; a custom description sets the voice; a custom description asking for rudeness never makes replies mean) and `evals/test_personality_routing.py` (personality requests reach the tool; `replyMode`, a friendly email and an app launch do not).
