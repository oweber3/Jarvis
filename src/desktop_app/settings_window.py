"""
Jarvis Settings Window

Auto-generated settings UI driven by config metadata.
Reads/writes config.json directly and groups settings by category.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QWidget,
    QLabel, QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox,
    QComboBox, QScrollArea, QGroupBox, QFormLayout, QGridLayout, QPushButton,
    QMessageBox, QSizePolicy, QListWidget, QListWidgetItem,
    QStackedWidget, QSplitter, QInputDialog, QFrame,
)
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QFont

from jarvis import credentials
from jarvis.config import (
    get_default_config, load_config,
    default_config_path, _save_json, _load_json, _move_secrets_to_store,
    SUPPORTED_CHAT_MODELS, TOOL_USE_CHAT_MODELS,
)
from jarvis.debug import debug_log
from desktop_app.themes import apply_theme, divider, hud_heading, link, set_role
from desktop_app.mcp_catalogue import CATALOGUE, CATALOGUE_BY_NAME, MCPEntry


# ---------------------------------------------------------------------------
# Config field metadata
# ---------------------------------------------------------------------------

@dataclass
class FieldMeta:
    """Metadata for a single config field."""
    key: str
    label: str
    description: str
    category: str
    field_type: str  # "bool", "int", "float", "str", "secret", "choice", "device", "list"
    choices: Optional[List[tuple[str, str]]] = None  # [(value, display), ...]
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    step: Optional[float] = None
    suffix: Optional[str] = None
    nullable: bool = False  # Whether None/"" is a valid value (shows "Default" option)
    section: str = ""  # Heading the field sits under on its page
    advanced: bool = False  # Shown only while "Show advanced settings" is on
    # Shown only while this returns True; it reads other fields' current values through ``get(key)``.
    shown_when: Optional[Callable[[Callable[[str], Any]], bool]] = None
    column: str = ""  # On a page with columns (Codex and Claude), the column the field sits in
    required: bool = False  # A text field that Save refuses to leave blank


# Categories and their display order
CATEGORIES = [
    ("general", "General"),
    ("listening", "Listening"),
    ("speaking", "Speaking"),
    ("models", "AI Models"),
    ("cloud", "Cloud Modes"),
    ("privacy", "Privacy"),
    ("memory", "Memory & Dictation"),
    ("extras", "Extras"),
    ("mcps", "MCP Servers"),
    ("advanced", "Advanced"),
]
# Listed in the sidebar only while advanced settings are shown.
ADVANCED_CATEGORY = "advanced"
# Window preference kept in config.json, written only while on.
SHOW_ADVANCED_KEY = "settings_show_advanced"

# Windows features configured only in config.json (no rows), named on the General page.
CONFIG_ONLY_WINDOWS_KEYS = (
    "windows_workspaces",
    "routines",
    "windows_app_aliases",
    "windows_path_aliases",
    "windows_monitor_aliases",
    "windows_audio_aliases",
    "windows_window_zones",
)
CONFIG_ONLY_WINDOWS_SPECS = (
    "src/jarvis/platform/windows/workspaces.spec.md",
    "src/jarvis/routines/routines.spec.md",
    "src/jarvis/platform/windows/apps_paths.spec.md",
)

# Models offered before Codex or Claude has ever reported its list. Once a bridge has checked in, the menu
# lists what that runtime reported instead (``_reported_model_choices``). The bridge preflight checks the
# chosen one against the live list, and an unlisted value set by hand is shown as an extra entry.
CODEX_MODEL_CHOICES = [
    ("gpt-6-luna", "GPT-6-Luna"),
    ("gpt-6-sol", "GPT-6-Sol"),
    ("gpt-6.1-sol", "GPT-6.1-Sol"),
    ("gpt-6-astra", "GPT-6-Astra"),
    ("gpt-5.6-luna", "GPT-5.6-Luna"),
    ("gpt-5.6-sol", "GPT-5.6-Sol"),
    ("gpt-5.6-terra", "GPT-5.6-Terra"),
]
CLAUDE_MODEL_CHOICES = [
    ("haiku", "Haiku (fastest)"),
    ("sonnet", "Sonnet"),
    ("opus", "Opus"),
    ("claude-fable-5-1[1m]", "Fable"),
    ("default", "Claude Code's choice"),
]


_CLOUD_MODEL_FIELDS = {"claude_model": "claude", "codex_model": "codex"}


def _reported_model_choices(fields: List["FieldMeta"], config_dir: Path) -> List["FieldMeta"]:
    """``fields`` with the Claude and Codex model menus listing what each runtime last reported, named with
    their versions ("Sonnet 5.5"). A mode that never reported keeps its built-in choices."""
    from jarvis.bridge.model_catalog import REPORTED_MODELS_FILE, load_reported_models
    path = config_dir / REPORTED_MODELS_FILE
    result = []
    for fm in fields:
        mode = _CLOUD_MODEL_FIELDS.get(fm.key)
        models = load_reported_models(mode, path) if mode else []
        result.append(replace(fm, choices=[(m.id, m.name) for m in models]) if models else fm)
    return result


def _decimals(step: Optional[float]) -> int:
    """Decimal places a number field shows: as many as its step has (10 shows 45, 0.05 shows 0.25)."""
    if not step:
        return 3
    text = f"{step:.6f}".rstrip("0")
    return len(text.split(".")[1])


def _is_default_value(val: Any, default_val: Any) -> bool:
    """True when ``val`` should be treated as the default and omitted from
    ``config.json`` (the minimal-config invariant).

    A value equal to the default is omitted. An emptied nullable field reads
    back as ``None``; treat that as the default when the default is itself
    empty (``""`` or ``None``) so we never persist a ``null`` for a field
    that would just fall back anyway.
    """
    if val == default_val:
        return True
    return val is None and default_val in (None, "")


def _dictation_hotkey_choices() -> list:
    """Build platform-aware dictation hotkey dropdown choices."""
    from jarvis.dictation.dictation_engine import format_hotkey_display
    from jarvis.config import _default_dictation_hotkey
    default = _default_dictation_hotkey()
    options = [
        ("ctrl+alt", format_hotkey_display("ctrl+alt")),
        ("ctrl+cmd", format_hotkey_display("ctrl+cmd")),
        ("ctrl+shift+d", format_hotkey_display("ctrl+shift+d")),
        ("ctrl+shift", format_hotkey_display("ctrl+shift")),
    ]
    return [
        (val, f"{label} (default)" if val == default else label)
        for val, label in options
    ]


def _on(key: str) -> Callable[[Callable[[str], Any]], bool]:
    """Shown while the switch ``key`` is on."""
    return lambda get: bool(get(key))


def _is(key: str, *values: Any) -> Callable[[Callable[[str], Any]], bool]:
    """Shown while ``key`` holds one of ``values``."""
    return lambda get: get(key) in values


def _all(*conditions: Callable[[Callable[[str], Any]], bool]) -> Callable[[Callable[[str], Any]], bool]:
    return lambda get: all(condition(get) for condition in conditions)


def _embedding_provider(get: Callable[[str], Any]) -> str:
    """The provider embeddings actually use: their own choice, otherwise the chat provider."""
    return get("embedding_provider") or get("llm_provider") or "ollama"


def _build_field_metadata() -> List[FieldMeta]:
    """Build the metadata registry for all user-facing config fields."""
    fields = []
    section = ""

    def heading(title: str) -> None:
        nonlocal section
        section = title

    def f(key, label, desc, cat, ftype, **kw):
        fields.append(FieldMeta(key=key, label=label, description=desc,
                                category=cat, field_type=ftype, section=section, **kw))

    model_choices = [(mid, info["name"]) for mid, info in SUPPORTED_CHAT_MODELS.items()]
    # The tested tool-use models are offered for every role so the tool model can also write replies
    # without a second model loading on each request (reply.spec.md, Tool-Model Mode).
    tool_use_choices = [(mid, info["name"]) for mid, info in TOOL_USE_CHAT_MODELS.items()]
    ollama_chat = _is("llm_provider", "ollama")
    openai_chat = _is("llm_provider", "openai_compatible")
    ollama_embeddings = lambda get: _embedding_provider(get) == "ollama"  # noqa: E731
    openai_embeddings = lambda get: _embedding_provider(get) == "openai_compatible"  # noqa: E731

    # --- General ---
    heading("Reply mode")
    f("reply_mode", "Reply Mode",
      "Who answers when Jarvis starts. Local keeps everything on this PC and works offline. Switch at any "
      "time from the tray or by saying \"use Claude\", \"use ChatGPT\" or \"go local\"; a cloud mode must "
      "also be allowed on the Cloud Modes page, otherwise Jarvis stays local.",
      "general", "choice",
      choices=[("local", "Local (offline, default)"), ("codex", "ChatGPT through Codex (cloud)"),
               ("claude", "Claude through Claude Code (cloud)")])
    heading("Windows control")
    f("fast_commands_enabled", "Quick Commands",
      "Run simple, unambiguous commands such as \"open Word\", \"set volume to 30%\" "
      "or \"what time is it\" instantly, without waiting for the AI model. "
      "Anything unclear still goes to the AI.",
      "general", "bool")
    f("windows_tools_enabled", "Windows Control Tools",
      "Let Jarvis open and arrange applications and windows, open folders, change the volume, control "
      "media and read system information. Off removes all of them.",
      "general", "bool")
    f("windows_fancyzones_enabled", "Use FancyZones Layouts",
      "Let window placement use the zone layouts you set up in Microsoft PowerToys FancyZones. "
      "Jarvis only reads them from this PC and never changes them.",
      "general", "bool")
    heading("Web search")
    f("web_search_enabled", "Web Search",
      "Let Jarvis search the web when it needs current information. Only the search words are sent, "
      "to the search engine.",
      "general", "bool")
    f("wikipedia_fallback_enabled", "Wikipedia Fallback",
      "Use Wikipedia as a last-resort source when other search engines fail. "
      "No key, no account, privacy-light.",
      "general", "bool", advanced=True, shown_when=_on("web_search_enabled"))
    f("brave_search_api_key", "Brave Search API Key",
      "Optional. When set, Brave is used as the primary fallback if DuckDuckGo "
      "is blocked. Free tier: 2,000 queries/month at api.search.brave.com. Kept in Windows Credential "
      "Manager, never in config.json; only whether one is stored is shown.",
      "general", "secret", advanced=True, shown_when=_on("web_search_enabled"))
    heading("Performance")
    f("low_power_mode", "Low Power Mode",
      "Use less memory and power: Jarvis skips loading AI models at start-up, and Ollama unloads them a "
      "minute after use instead of keeping them ready. The first reply after a pause is slower.",
      "general", "bool")

    # --- Listening ---
    heading("Microphone")
    f("voice_device", "Microphone",
      "Which microphone Jarvis listens on. System Default follows Windows.",
      "listening", "device")
    f("sample_rate", "Sample Rate",
      "How often the microphone is sampled. 16000 Hz is what speech recognition expects; change it only "
      "if your microphone refuses that rate.",
      "listening", "choice", advanced=True,
      choices=[("16000", "16000 Hz"), ("44100", "44100 Hz"), ("48000", "48000 Hz")])
    heading("Wake word")
    f("wake_word", "Wake Word",
      "The word that gets Jarvis's attention. It cannot be left empty.",
      "listening", "str", required=True)
    f("wake_fuzzy_ratio", "Wake Word Strictness",
      "How closely what you say must match the wake word, from 0.5 (loose) to 1.0 (exact). Raise it if "
      "Jarvis wakes by mistake; lower it if it misses you.",
      "listening", "float", min_val=0.5, max_val=1.0, step=0.01)
    f("wake_sound_enabled", "React to the Sound of the Wake Word",
      "Light up as soon as the wake word is heard, before you finish speaking. What you said still "
      "decides whether Jarvis answers. Runs locally on the CPU. Takes effect after Jarvis restarts.",
      "listening", "bool")
    f("wake_sound_threshold", "Wake Sound Sensitivity",
      "Lower reacts more readily but also to sound-alike words, from 0.05 to 0.95. Takes effect after "
      "Jarvis restarts.",
      "listening", "float", min_val=0.05, max_val=0.95, step=0.05, shown_when=_on("wake_sound_enabled"))
    f("voice_wake_wait_seconds", "Wait After Wake Word",
      "How long Jarvis waits for your request when you say only the wake word and pause.",
      "listening", "float", min_val=1.0, max_val=30.0, step=0.5, suffix="s")
    heading("Conversation")
    f("voice_collect_seconds", "Pause Before Sending",
      "How long Jarvis waits after you stop talking before it answers. Raise it if Jarvis cuts you off "
      "mid-sentence.",
      "listening", "float", min_val=0.5, max_val=30.0, step=0.1, suffix="s")
    f("hot_window_enabled", "Follow-up Listening",
      "Keep listening for a few seconds after each reply, so you can carry on without saying the wake "
      "word again.",
      "listening", "bool")
    f("hot_window_seconds", "Follow-up Time",
      "How long Jarvis keeps listening for a follow-up after a reply.",
      "listening", "float", min_val=1.0, max_val=30.0, step=0.5, suffix="s",
      shown_when=_on("hot_window_enabled"))
    f("wake_overlay_enabled", "Wake Screen Effect",
      "Fill the edges of every screen with soft light that breathes and pulses while Jarvis is awake, "
      "from the wake word until the conversation ends. Click-through and never takes focus; screens showing a "
      "full-screen app are left alone.",
      "listening", "bool")
    heading("Your voice")
    f("speaker_verification", "Speaker Verification",
      "Only respond to your own voice. Soft ignores voices that are confidently someone else; "
      "strict needs your voice. Requires an enrolled voice (see below).",
      "listening", "choice",
      choices=[("off", "Off"), ("soft", "Soft: ignore other voices"), ("strict", "Strict: owner only")])
    f("barge_in_enabled", "Talk Over Jarvis",
      "Lower Jarvis's voice as soon as your enrolled voice is heard over it.",
      "listening", "bool", shown_when=_is("speaker_verification", "soft", "strict"))

    # --- Speaking ---
    heading("Voice")
    f("tts_enabled", "Speak Replies",
      "Read replies aloud. Off keeps Jarvis silent; replies still appear in the chat.",
      "speaking", "bool")
    f("tts_pc_speakers_enabled", "Voice on This PC",
      "Play Jarvis's voice through this PC's speakers or headphones. Turn it off to speak only through "
      "another voice output, such as one added by an extension.",
      "speaking", "bool", shown_when=_on("tts_enabled"))
    f("tts_engine", "Voice Engine",
      "Piper is fast and light. Chatterbox can clone a voice from a recording but needs a strong PC, "
      "ideally an NVIDIA graphics card.",
      "speaking", "choice", choices=[("piper", "Piper (fast)"), ("chatterbox", "Chatterbox (voice cloning)")],
      shown_when=_on("tts_enabled"))
    piper = _all(_on("tts_enabled"), _is("tts_engine", "piper"))
    chatterbox = _all(_on("tts_enabled"), _is("tts_engine", "chatterbox"))
    heading("Piper voice")
    f("tts_piper_length_scale", "Speaking Speed",
      "Below 1.0 speaks faster, above 1.0 slower.",
      "speaking", "float", min_val=0.1, max_val=3.0, step=0.05, shown_when=piper)
    f("tts_piper_sentence_silence", "Pause Between Sentences",
      "Silence after each sentence.",
      "speaking", "float", min_val=0.0, max_val=2.0, step=0.05, suffix="s", shown_when=piper)
    f("tts_piper_noise_scale", "Expressiveness",
      "Higher varies the tone more; lower sounds flatter and steadier.",
      "speaking", "float", min_val=0.0, max_val=2.0, step=0.05, advanced=True, shown_when=piper)
    f("tts_piper_noise_w", "Rhythm Variation",
      "Higher gives a livelier, less even rhythm.",
      "speaking", "float", min_val=0.0, max_val=2.0, step=0.05, advanced=True, shown_when=piper)
    f("tts_piper_model_path", "Custom Voice File",
      "Full path to a Piper .onnx voice. Leave empty for the built-in voice.",
      "speaking", "str", nullable=True, advanced=True, shown_when=piper)
    f("tts_piper_speaker", "Speaker Number",
      "For voice files that contain several speakers: which one to use.",
      "speaking", "int", min_val=0, max_val=99, nullable=True, advanced=True, shown_when=piper)
    heading("Chatterbox voice")
    f("tts_chatterbox_audio_prompt", "Voice to Clone",
      "Full path to a short, clear recording of the voice to copy. Leave empty for Chatterbox's own voice.",
      "speaking", "str", nullable=True, shown_when=chatterbox)
    f("tts_chatterbox_exaggeration", "Emotion",
      "How much feeling goes into the voice. 0.5 is natural; higher is more dramatic.",
      "speaking", "float", min_val=0.0, max_val=2.0, step=0.05, shown_when=chatterbox)
    f("tts_chatterbox_cfg_weight", "Pacing",
      "How closely speech follows the text and the cloned voice. Lower gives slower, more deliberate "
      "speech; 0.5 suits most voices.",
      "speaking", "float", min_val=0.0, max_val=2.0, step=0.05, advanced=True, shown_when=chatterbox)
    f("tts_chatterbox_device", "Runs On",
      "Chatterbox runs best on an NVIDIA graphics card (CUDA). Auto picks the card when there is one.",
      "speaking", "choice", choices=[("cuda", "Graphics card (CUDA)"), ("auto", "Auto"), ("cpu", "Processor")],
      advanced=True, shown_when=chatterbox)

    # --- AI Models ---
    heading("Provider")
    f("llm_provider", "Provider",
      "Where the AI model runs. Ollama is the default. Choose an OpenAI-compatible server for LM Studio, "
      "llama.cpp, vLLM and similar.",
      "models", "choice",
      choices=[("ollama", "Ollama (local)"),
               ("openai_compatible", "OpenAI-compatible server")])
    f("ollama_base_url", "Ollama Address",
      "Where Ollama is running. The default is this PC.",
      "models", "str", advanced=True, shown_when=lambda get: ollama_chat(get) or ollama_embeddings(get))
    f("llm_base_url", "Server Address",
      "The server's API address, such as http://localhost:1234/v1 for LM Studio.",
      "models", "str", nullable=True, shown_when=openai_chat)
    f("llm_api_key", "API Key",
      "Only if the server needs one. Kept in Windows Credential Manager, never in config.json; only whether "
      "one is stored is shown.",
      "models", "secret", shown_when=openai_chat)
    heading("Models")
    f("ollama_chat_model", "Chat Model",
      "The model that writes Jarvis's replies.",
      "models", "choice", choices=model_choices + tool_use_choices, shown_when=ollama_chat)
    f("llm_chat_model", "Chat Model",
      "The name of the model on the server that writes Jarvis's replies. Leave empty to use the same name "
      "as the Ollama chat model.",
      "models", "str", nullable=True, shown_when=openai_chat)
    f("fast_model", "Fast Model",
      "A small, quick model for real-time work such as deciding whether you spoke to Jarvis and which "
      "tool fits. Automatic picks the right one for your provider.",
      "models", "choice", choices=[("", "Automatic (recommended)")] + model_choices + tool_use_choices)
    f("tool_model", "Tool Model",
      "Optional separate model that chooses and calls tools while the chat model writes the reply. "
      "GPT-OSS 20B chose tools best in testing but needs about 13 GB, so the two models take turns in memory "
      "unless both fit on your graphics card. Off lets the chat model do both.",
      "models", "choice",
      choices=[("", "Off (chat model uses tools)")] + tool_use_choices + model_choices)
    f("llm_thinking_enabled", "Think Before Answering",
      "Let the chat model reason before it replies. Slower, sometimes better answers.",
      "models", "bool")
    f("llm_chat_timeout_sec", "Answer Time Limit",
      "How long to wait for the chat model's reply before giving up.",
      "models", "float", min_val=10, max_val=600, step=10, suffix="s")
    heading("Embeddings")
    f("embedding_provider", "Embedding Provider",
      "Embeddings let Jarvis search its memory. They use the chat provider unless you pick another, for "
      "example Ollama when your chat server has no embeddings.",
      "models", "choice", advanced=True,
      choices=[("", "Same as chat provider"),
               ("ollama", "Ollama (local)"),
               ("openai_compatible", "OpenAI-compatible server")])
    f("ollama_embed_model", "Embedding Model",
      "The Ollama model used to search memory. Changing it makes older memories harder to find until "
      "they are processed again.",
      "models", "str", advanced=True, shown_when=ollama_embeddings)
    f("embedding_model", "Embedding Model",
      "The server's embedding model name. Leave empty to use the same name as the Ollama embedding model.",
      "models", "str", nullable=True, shown_when=openai_embeddings)
    f("embedding_base_url", "Embedding Server Address",
      "Only if embeddings come from a different server than chat. Leave empty to use the chat server.",
      "models", "str", nullable=True, shown_when=openai_embeddings)
    f("embedding_api_key", "Embedding API Key",
      "Only if the embedding server needs a different key; without one the chat key is used. Kept in Windows "
      "Credential Manager, never in config.json; only whether one is stored is shown.",
      "models", "secret", shown_when=openai_embeddings)

    # --- Cloud Modes: Codex and Claude side by side, one description per row ---
    for mode, name in (("codex", "Codex"), ("claude", "Claude")):
        def cloud(key, label, desc, ftype, **kw):
            f(f"{mode}_{key}", label, desc, "cloud", ftype, column=name, **kw)

        allowed = _on(f"{mode}_enabled")
        heading("Access")
        cloud("enabled", "Allow",
              "Lets Jarvis switch to this mode. A hidden Codex or Claude Code process then interprets requests and "
              "uses Jarvis's tools: the request, the context switched on below and tool results are sent to "
              "OpenAI (Codex, with your ChatGPT sign-in) or Anthropic (Claude, with your Claude subscription "
              "sign-in, never an API key). Off means nothing can switch Jarvis to it.",
              "bool")
        heading("Model")
        cloud("model", "Model",
              "The model each mode answers with. Jarvis checks that your sign-in offers it and never substitutes "
              "another model. A model set by hand in config.json is shown and kept.",
              "choice", choices=CODEX_MODEL_CHOICES if mode == "codex" else CLAUDE_MODEL_CHOICES,
              shown_when=allowed)
        effort_choices = [("low", "Low (fastest)"), ("medium", "Medium"), ("high", "High"),
                          ("xhigh", "Extra high"), ("max", "Maximum")]
        effort_choices += [("ultra", "Ultra")] if mode == "codex" else [("", "Model default")]
        cloud("reasoning_effort" if mode == "codex" else "effort", "Effort",
              "Lower is faster. Jarvis checks that the model supports it and never substitutes another value; "
              "Claude models without effort levels ignore it.",
              "choice", choices=effort_choices, shown_when=allowed)
        heading("What it may see")
        cloud("share_recent_dialogue", "Recent Conversation",
              "Send the last few messages so follow-ups make sense. They are redacted and bounded. When off, "
              "every request starts with no earlier conversation.",
              "bool", shown_when=allowed)
        cloud("recent_dialogue_messages", "Messages Shared",
              "How many recent messages are shared.",
              "int", min_val=0, max_val=20, step=1,
              shown_when=_all(allowed, _on(f"{mode}_share_recent_dialogue")))
        cloud("share_foreground_window", "The Window You're Looking At",
              "With each request you make at the PC, send which window is in front (application, window handle, "
              "display, state), so \"close this\" acts on it. Never its title, and never for requests from your "
              "phone. When off, the model has to ask or look the window up with a tool.",
              "bool", shown_when=allowed)
        cloud("share_desktop_referents", "Recent Desktop Actions",
              "Send Jarvis's own record of what it acted on in this conversation: the last few windows it "
              "opened, focused or placed (application, window handle, display, zone, state), the device it "
              "controlled, the media player and what kind of thing is on the clipboard, so \"move it\" or "
              "\"pause it\" acts on the right thing. No window titles, paths, track names, clipboard contents "
              "or conversation text. When off, follow-ups need the name of what they are about.",
              "bool", shown_when=allowed)
        cloud("share_long_term_memory", "Your Memories",
              "Off by default: nothing from your diary, memory graph or meal log is ever disclosed. Turn on to "
              "let the model use the tools that read them.",
              "bool", shown_when=allowed)
        heading("Limits")
        cloud("timeout_sec", "Answer Time Limit",
              "How long to wait for a request to finish.",
              "float", min_val=5, max_val=600, step=5, suffix="s", advanced=True, shown_when=allowed)
        cloud("queue_limit", "Queued Requests",
              "Requests allowed to wait behind the one in progress. More are refused as busy.",
              "int", min_val=0, max_val=5, step=1, advanced=True, shown_when=allowed)
        cloud("max_tool_calls", "Tool Calls per Request",
              "The most tool calls the model may make for one request.",
              "int", min_val=1, max_val=20, step=1, advanced=True, shown_when=allowed)
        cloud("executable", "Program",
              "Leave as codex or claude to use the installed program (on PATH, or where the Codex app or the "
              "Claude Code installer put it), or enter the full path to codex.exe or claude.exe.",
              "str", advanced=True, shown_when=allowed)

    # --- Privacy ---
    heading("Screen and files")
    f("screen_awareness_enabled", "Screen Awareness",
      "Let Jarvis look at your screen when you ask about it (\"what's on my screen?\", \"what does this "
      "error mean?\"). It captures the screen only for that request, reads its text on this PC and never "
      "stores it. In Codex or Claude mode the screen you asked about is sent to that model. Turn this off "
      "and Jarvis never looks at the screen. Takes effect after Jarvis restarts.",
      "privacy", "bool")
    f("file_delete_enabled", "Allow File Deletion",
      "Let Jarvis delete a file in your home folder when you ask, after you confirm it. Off means Jarvis "
      "cannot delete files at all, whatever is asked and in every reply mode. Moving, copying and "
      "renaming are not affected. Takes effect after Jarvis restarts.",
      "privacy", "bool")
    heading("Activity log")
    logging = _on("activity_log_enabled")
    f("activity_log_enabled", "Record Activity",
      "Off by default. When on, Jarvis records which application is in the foreground and its window "
      "title (with emails, tokens and similar secrets removed), and when you are idle, so you can ask "
      "what you were working on. It is stored only in Jarvis's database on this PC. It never records "
      "keystrokes, screen contents, files, or password managers and private browsing windows. Pause or "
      "delete it from the tray at any time. Takes effect after Jarvis restarts.",
      "privacy", "bool")
    f("activity_log_paused", "Paused",
      "Stop recording without turning the log off. Also available from the tray.",
      "privacy", "bool", shown_when=logging)
    f("activity_log_retention_days", "Keep History",
      "Days of history to keep. Older sessions are deleted once a day.",
      "privacy", "int", min_val=1, max_val=365, step=1, suffix="days", shown_when=logging)
    f("activity_log_excluded_processes", "Never Record These Applications",
      "Executable names that are never recorded (not even that they were used). Defaults to common "
      "password managers. Replacing this list replaces the defaults.",
      "privacy", "list", shown_when=logging)
    f("activity_log_private_title_markers", "Never Record Windows Containing",
      "Window titles containing any of these words are never recorded. Defaults to the private-browsing "
      "markers of common browsers in many languages.",
      "privacy", "list", shown_when=logging)
    f("activity_log_share_with_cloud", "Share With Codex and Claude",
      "Off by default: the activity log is available only to the local model, and never leaves this PC. "
      "Turn on to let the Codex and Claude reply modes read it, which sends the parts they ask for "
      "(application names and window titles) to OpenAI or Anthropic.",
      "privacy", "bool", shown_when=logging)
    f("activity_log_idle_after_sec", "Idle After",
      "Time without keyboard or mouse input before time is recorded as idle instead of as the "
      "application you left open.",
      "privacy", "float", min_val=30, max_val=3600, step=30, suffix="s", advanced=True, shown_when=logging)
    heading("Calendar and email")
    calendar_on = _on("calendar_enabled")
    email_on = _on("email_enabled")
    f("calendar_enabled", "Read My Calendar",
      "Off by default. Let Jarvis read your calendars when you ask (\"what's on today?\", \"when is my next "
      "meeting?\"). Read only: it never adds or changes events. Add a Google or Outlook.com calendar by its secret "
      "iCal address, or classic Outlook on this PC, with python -m jarvis.agenda setup. Addresses are kept in "
      "Windows Credential Manager, never in the settings file. Takes effect after Jarvis restarts.",
      "privacy", "bool")
    f("calendar_cache_minutes", "Reuse Calendar For",
      "Minutes a downloaded calendar is reused before it is fetched again. When offline, Jarvis answers from "
      "the last copy and says so.",
      "privacy", "int", min_val=0, max_val=1440, step=5, suffix="min", advanced=True, shown_when=calendar_on)
    f("calendar_share_with_cloud", "Share Calendar With Codex and Claude",
      "Off by default: your calendar is available only to the local model and never leaves this PC. Turn on "
      "to let the Codex and Claude reply modes read it, which sends the events they ask for to OpenAI or "
      "Anthropic.",
      "privacy", "bool", shown_when=calendar_on)
    f("email_enabled", "Read My Email",
      "Off by default. Let Jarvis check your inbox when you ask (\"any new emails?\", \"read me the latest "
      "email from Sam\"). Read only: it never sends, deletes or marks anything as read. Add Gmail (with an app "
      "password), classic Outlook on this PC or an Outlook.com account with python -m jarvis.mail setup. "
      "Passwords and sign-ins are kept in Windows Credential Manager. Takes effect after Jarvis restarts.",
      "privacy", "bool")
    f("email_share_with_cloud", "Share Email With Codex and Claude",
      "Off by default: your email is available only to the local model and never leaves this PC. Turn on to "
      "let the Codex and Claude reply modes read it, which sends the messages they ask for to OpenAI or "
      "Anthropic.",
      "privacy", "bool", shown_when=email_on)
    heading("Location")
    located = _on("location_enabled")
    f("location_enabled", "Use Location",
      "Let Jarvis use your approximate location for weather, local time and nearby answers. It is worked "
      "out on this PC from your internet address, using the GeoLite2 location database you add in Setup.",
      "privacy", "bool")
    f("location_auto_detect", "Detect Automatically",
      "Work out your location from this PC's internet address. Off uses only the address entered below.",
      "privacy", "bool", shown_when=located)
    f("location_ip_address", "Internet Address to Use",
      "An internet (IP) address to locate instead of this PC's. Leave empty to use this PC's address while "
      "Detect Automatically is on.",
      "privacy", "str", nullable=True, shown_when=located)
    f("location_cache_minutes", "Recheck Location Every",
      "How long a detected location is reused before it is worked out again.",
      "privacy", "int", min_val=1, max_val=1440, step=5, suffix="min", advanced=True, shown_when=located)
    f("location_cgnat_resolve_public_ip", "Look Up Public Address",
      "Some internet providers share one address among many homes, which hides your real one. When on, "
      "Jarvis asks a public service for your address so the location is right.",
      "privacy", "bool", advanced=True, shown_when=located)

    # --- Memory & Dictation ---
    heading("Memory")
    f("dialogue_memory_timeout", "Conversation Ends After",
      "Idle time after which a conversation is written to the diary and Jarvis forgets what \"it\" and "
      "\"that\" referred to.",
      "memory", "float", min_val=30, max_val=3600, step=30, suffix="s")
    f("memory_enrichment_max_results", "Memories per Reply",
      "How many past memories are added to each reply.",
      "memory", "int", min_val=1, max_val=50)
    f("memory_enrichment_source", "Where Memories Come From",
      "The diary holds summaries of past conversations; the memory graph holds facts sorted by topic.",
      "memory", "choice", advanced=True,
      choices=[("diary", "Diary only"), ("graph", "Memory graph only"), ("all", "Both")])
    heading("Dictation")
    dictating = _on("dictation_enabled")
    f("dictation_enabled", "Dictation",
      "Hold a hotkey to record speech, release to paste what you said into any app.",
      "memory", "bool")
    f("dictation_hotkey", "Dictation Hotkey",
      "Key combination to hold for dictation. Double-tap it for hands-free dictation.",
      "memory", "choice", choices=_dictation_hotkey_choices(), shown_when=dictating)
    f("dictation_custom_dictionary", "Custom Words",
      "Corrections applied to dictation, one per line as 'wrong -> right' (e.g. 'Jarvice -> Jarvis').",
      "memory", "list", shown_when=dictating)
    f("dictation_filler_removal", "Remove Filler Words",
      "Use the local AI model to remove filler words (um, uh, like) from dictation.",
      "memory", "bool", shown_when=dictating)
    f("dictation_thinking_enabled", "Think While Cleaning Up",
      "Let the model reason while removing filler words. Better results, slower paste.",
      "memory", "bool", shown_when=_all(dictating, _on("dictation_filler_removal")))

    # --- Extras ---
    heading("Phone access")
    phone = _on("remote_access_enabled")
    f("remote_access_enabled", "Allow Phone Access",
      "Reach Jarvis from your phone: the orb, the conversation and desktop confirmations in your phone's "
      "browser. Jarvis serves the page itself on this PC and answers only phones you pair, on your home "
      "network or your own VPN (such as Tailscale). Pair a phone from the tray: Phone Access. "
      "Restart Jarvis after changing it.",
      "extras", "bool")
    f("remote_access_allow_confirm", "Approve From the Phone",
      "Let a paired phone approve or deny actions that ask for confirmation on the desktop, such as "
      "deleting a file. Off keeps those answers on this PC.",
      "extras", "bool", shown_when=phone)
    f("remote_access_quick_actions", "Quick Actions",
      "Commands shown as one-tap buttons on the phone, one per line. Each is sent as if typed.",
      "extras", "list", shown_when=phone)
    f("remote_access_host", "Listen Address",
      "0.0.0.0 listens on every network of this PC; only private addresses are ever answered. Enter one "
      "address, such as this PC's Tailscale address, to answer on that network only.",
      "extras", "str", advanced=True, shown_when=phone)
    f("remote_access_port", "Phone Port",
      "The port the phone connects to, as in http://<this PC>:8765/.",
      "extras", "int", min_val=1024, max_val=65535, step=1, advanced=True, shown_when=phone)
    heading("Web chat")
    f("web_chat_enabled", "Use Web Chat",
      "A chat window with projects and a model picker. Jarvis serves the page itself on this PC, on the "
      "loopback address only, and keeps your chats (redacted text) in its own database. The tray's Chat "
      "entry opens it instead of the classic chat. Restart Jarvis after changing it.",
      "extras", "bool")
    f("web_chat_port", "Web Chat Port",
      "The local port the page is served on (127.0.0.1 only).",
      "extras", "int", min_val=1024, max_val=65535, step=1, advanced=True, shown_when=_on("web_chat_enabled"))
    heading("TV")
    f("roku_host", "Roku TV Address",
      "The Roku TV's address on your home network, such as 192.168.1.50, to control it by voice. Empty turns TV "
      "control off. Only private home-network addresses are accepted. Reserve the address for the TV in your "
      "router so it does not change, and turn on Settings > System > Advanced system settings > Control by "
      "mobile apps > Network access (Default or Permissive) on the TV. To find the address, run "
      "python -m jarvis.devices.roku --discover. Restart Jarvis after changing it.",
      "extras", "str", nullable=True)
    heading("Gesture control")
    gestures = _on("gesture_snap_enabled")
    f("gesture_snap_enabled", "Allow Gesture Control",
      "Move and scroll windows with your hand through the webcam, palm towards the camera: raise one "
      "finger to aim the pointer, make a fist to grab the window under it and move it, then raise a finger or open "
      "your hand to snap it into the FancyZones zone under the pointer. Raise two fingers and move your hand up or "
      "down to scroll. Open your hand flat to pause. It never clicks. This only makes it available: say \"start "
      "gesture control\" to turn the camera on and \"stop gesture control\" to turn it off. Everything stays on "
      "this PC. Takes effect after Jarvis restarts.",
      "extras", "bool")
    f("gesture_snap_camera", "Camera",
      "Which webcam to use: 0 is the first one. Run python -m jarvis.gestures --camera N --preview to try one.",
      "extras", "int", min_val=0, max_val=16, shown_when=gestures)
    f("gesture_snap_hand", "Hand",
      "Which hand aims. Any: whichever hand is in view, and the one already in use keeps control.",
      "extras", "choice", choices=[("any", "Any hand"), ("right", "Right hand"), ("left", "Left hand")],
      shown_when=gestures)
    f("gesture_snap_pointer_ring", "Pointer Ring",
      "Show a small glowing ring round the mouse pointer while gesture control is on, so the pointer is easy to "
      "follow. Clicks pass through it and it never takes focus. Applies as soon as Settings are saved.",
      "extras", "bool", shown_when=gestures)
    f("gesture_snap_comfort_box", "Reach",
      "How much of the camera's view stands for your whole desktop (all monitors), as a share of its width, "
      "centred where you raise your hand. Smaller means less arm movement but a more sensitive pointer.",
      "extras", "float", min_val=0.15, max_val=1.0, step=0.05, shown_when=gestures)
    f("gesture_snap_scroll_speed", "Scroll Speed",
      "How fast two-finger scrolling goes the further your hand moves from where the scroll started.",
      "extras", "float", min_val=0.5, max_val=60.0, step=0.5, shown_when=gestures)
    f("gesture_snap_finger_up", "Finger Raise Point",
      "How straight a finger must be to count as raised. A straight finger reads about 1.3; lower counts a "
      "finger as raised sooner.",
      "extras", "float", min_val=0.8, max_val=2.0, step=0.01, advanced=True, shown_when=gestures)
    f("gesture_snap_finger_down", "Finger Curl Point",
      "How curled a finger must be to count as lowered. A curled finger reads about 0.7. Must be below the "
      "raise point; between the two a finger keeps its last state, so it never flickers.",
      "extras", "float", min_val=0.5, max_val=1.5, step=0.01, advanced=True, shown_when=gestures)
    f("gesture_snap_debounce_ms", "Hold Before It Counts",
      "How long a gesture (one finger, two fingers, a fist or an open hand) must hold to count, so brief "
      "tracking glitches never grab, drop or scroll.",
      "extras", "int", min_val=0, max_val=500, step=10, suffix="ms", advanced=True, shown_when=gestures)
    f("gesture_snap_smoothing", "Pointer Steadiness",
      "How much the pointer is smoothed while your hand is still. Lower is steadier but feels slower.",
      "extras", "float", min_val=0.05, max_val=10.0, step=0.05, suffix="Hz", advanced=True, shown_when=gestures)
    f("gesture_snap_speed_response", "Fast-Move Response",
      "How quickly smoothing gives way when you move fast. Higher keeps up with quick movements better but "
      "lets more shake through.",
      "extras", "float", min_val=0.0, max_val=0.1, step=0.001, advanced=True, shown_when=gestures)

    # --- Advanced (every field here is advanced) ---
    def adv(key, label, desc, ftype, **kw):
        f(key, label, desc, "advanced", ftype, advanced=True, **kw)

    heading("Speech recognition")
    adv("whisper_model", "Recognition Model",
        "Bigger models understand more accurately but are slower and use more memory.",
        "choice", choices=[("tiny", "Tiny"), ("base", "Base"), ("small", "Small"),
                           ("medium", "Medium"), ("large-v3", "Large v3")])
    # MLX runs only on Apple Silicon; a value set by hand elsewhere still shows (see the choice widget).
    whisper_backend_choices = [("auto", "Auto")]
    if sys.platform == "darwin":
        whisper_backend_choices.append(("mlx", "MLX (Apple Silicon)"))
    whisper_backend_choices.append(("faster-whisper", "Faster Whisper"))
    adv("whisper_backend", "Recognition Engine",
        "Auto picks the right engine for this PC.",
        "choice", choices=whisper_backend_choices)
    adv("whisper_device", "Recognition Runs On",
        "Auto uses an NVIDIA graphics card (CUDA) when there is one, otherwise the processor.",
        "choice", choices=[("auto", "Auto"), ("cuda", "Graphics card (CUDA)"), ("cpu", "Processor")])
    adv("whisper_compute_type", "Recognition Precision",
        "INT8 is fastest and lightest; Float16 and Float32 are slightly more accurate and need more memory.",
        "choice", choices=[("int8", "INT8 (fastest)"), ("float16", "Float16"), ("float32", "Float32")])
    adv("whisper_min_confidence", "Minimum Confidence",
        "Words recognised with less confidence than this are dropped, which stops made-up text from "
        "background noise.",
        "float", min_val=0.0, max_val=1.0, step=0.05)
    adv("whisper_no_speech_threshold", "Silence Filter",
        "Text recognised from audio that is probably silence is dropped when that chance is at or above this "
        "value. Lower drops more.",
        "float", min_val=0.0, max_val=1.0, step=0.05)
    heading("When Jarvis stops listening")
    adv("vad_enabled", "Speech Detection",
        "Separate speech from silence and background noise before recognising it.",
        "bool")
    adv("vad_aggressiveness", "Noise Filtering",
        "How strictly background noise is filtered out, from 0 (least) to 3 (most).",
        "int", min_val=0, max_val=3, shown_when=_on("vad_enabled"))
    adv("voice_min_energy", "Minimum Speech Loudness",
        "Used only while Speech Detection is off, or if it fails: sounds quieter than this are ignored.",
        "float", min_val=0.0, max_val=1.0, step=0.005)
    adv("endpoint_silence_ms", "Silence That Ends a Phrase",
        "How much silence ends one phrase of speech.",
        "int", min_val=100, max_val=5000, step=50, suffix="ms")
    adv("max_utterance_ms", "Longest Phrase",
        "A single phrase is cut off after this long.",
        "int", min_val=1000, max_val=60000, step=1000, suffix="ms")
    adv("tts_max_utterance_ms", "Longest Phrase While Speaking",
        "A shorter cut-off while Jarvis is talking, so \"stop\" is heard quickly.",
        "int", min_val=500, max_val=10000, step=500, suffix="ms")
    adv("voice_max_collect_seconds", "Longest Request",
        "The most speech collected for one request.",
        "float", min_val=10.0, max_val=600.0, step=10, suffix="s")
    adv("transcript_buffer_duration_sec", "Recent Speech Kept",
        "How much recent speech the wake check can look back over.",
        "float", min_val=10, max_val=600, step=10, suffix="s")
    heading("Echo detection")
    adv("echo_tolerance", "Echo Timing Tolerance",
        "How far out of step with Jarvis's voice a sound can be and still count as its echo. Follow-up "
        "listening also starts this long after a reply.",
        "float", min_val=0.0, max_val=2.0, step=0.05, suffix="s")
    adv("tts_rate", "Estimated Speaking Rate",
        "Roughly how many words a minute Jarvis speaks. It does not change the voice's speed (that is "
        "Speaking Speed); echo detection uses it to tell which words Jarvis was saying.",
        "int", min_val=80, max_val=400, step=10, suffix="WPM", nullable=True)
    heading("Wake check")
    adv("intent_judge_thinking_enabled", "Think During Wake Check",
        "Let the quick \"was that meant for me?\" check reason first. More accurate, slower to respond.",
        "bool")
    adv("intent_judge_timeout_sec", "Wake Check Time Limit",
        "How long the wake check may take.",
        "float", min_val=1, max_val=30, step=0.5, suffix="s")
    heading("Time limits")
    adv("llm_routing_timeout_sec", "Routing Time Limit",
        "How long quick model calls may take: choosing tools, picking memory search words, and warming "
        "each model up at start-up.",
        "float", min_val=1, max_val=600, step=1, suffix="s")
    adv("llm_tools_timeout_sec", "Time and Weather Time Limit",
        "How long the weather tool waits for the model to find the place in your request, and the time "
        "tool waits for its place look-up.",
        "float", min_val=10, max_val=600, step=10, suffix="s")
    adv("llm_embedding_timeout_sec", "Memory Search Time Limit",
        "How long a memory search (embedding) may take.",
        "float", min_val=5, max_val=300, step=5, suffix="s")
    heading("Local model")
    adv("llm_num_ctx", "Context Size",
        "How much text Ollama can consider at once. Larger remembers more of the conversation but uses more "
        "memory.",
        "int", min_val=512, max_val=262144, step=1024, shown_when=ollama_chat)
    adv("llm_keep_alive", "Keep Model Loaded For",
        "How long Ollama keeps a model loaded after a request: a duration such as 30m or 2h, or -1 to keep "
        "it loaded. Longer avoids reload delays and holds memory. Low Power Mode uses 1m.",
        "str", shown_when=ollama_chat)
    heading("Tool use")
    adv("planner_enabled", "Plan Multi-Step Requests",
        "Break requests with several parts into steps before acting on them.",
        "bool")
    adv("tool_selection_strategy", "How Tools Are Chosen",
        "How Jarvis narrows its tools down for each request. The model choosing is the most accurate.",
        "choice", choices=[("llm", "The model chooses"), ("embedding", "By meaning"),
                           ("keyword", "By keyword"), ("all", "Offer every tool")])
    adv("agentic_max_turns", "Most Steps per Request",
        "How many tool steps Jarvis may take before it must answer.",
        "int", min_val=1, max_val=30)
    adv("tool_carryover_max_turns", "Tool Results Remembered",
        "How many earlier replies' tool results stay available for follow-up questions.",
        "int", min_val=0, max_val=10)
    adv("tool_carryover_per_entry_chars", "Tool Result Length Remembered",
        "How many characters of each remembered tool result are kept.",
        "int", min_val=200, max_val=8000, step=100)

    return fields


FIELD_METADATA = _build_field_metadata()


def _extension_settings(config: Dict[str, Any]) -> tuple[List[FieldMeta], List[tuple[str, str]], Dict[str, Any]]:
    """Fields, pages and defaults of the enabled extensions (``extensions/extensions.spec.md``, Settings)."""
    try:
        from types import SimpleNamespace
        from jarvis.extensions import load_extensions

        enabled = config.get("extensions_enabled")
        folder = config.get("extensions_dir")
        # register() sees every config.json value (with defaults) as an attribute of api.config.
        cfg = SimpleNamespace(**{key: value for key, value in config.items() if key.isidentifier()})
        cfg.extensions_enabled = [n for n in enabled if isinstance(n, str)] if isinstance(enabled, list) else []
        cfg.extensions_dir = folder if isinstance(folder, str) else ""
        loaded = load_extensions(cfg, raw=config, quiet=True)
    except Exception as e:
        debug_log(f"extension settings unavailable: {type(e).__name__}", "settings")
        return [], [], {}
    fields: List[FieldMeta] = []
    pages: List[tuple[str, str]] = []
    for page in loaded.settings_pages():
        category = f"extension:{page.extension}"
        pages.append((category, page.label))
        for item in page.fields:
            fields.append(FieldMeta(key=item.key, label=item.label, description=item.description,
                                    category=category, field_type=item.type,
                                    choices=list(item.choices) if item.choices else None,
                                    min_val=item.min, max_val=item.max, step=item.step, suffix=item.suffix,
                                    nullable=item.nullable))
    # A key is shown once: a built-in field wins, then the first extension that declared it.
    known = {fm.key for fm in FIELD_METADATA}
    unique = []
    for fm in fields:
        if fm.key not in known:
            known.add(fm.key)
            unique.append(fm)
    fields = unique
    return fields, pages, loaded.setting_defaults()


# ---------------------------------------------------------------------------
# Windows Control status (read-only information shown beside the toggles)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowsCapability:
    """A group of Windows-control abilities and whether it is usable."""
    label: str
    available: bool


# One label per capability group, matching the tools registered by
# ``jarvis.tools.registry.configure_windows_tools``.
_WINDOWS_CAPABILITY_GROUPS = [
    "Applications and windows",
    "Volume and media",
    "System information",
    "Files and folders",
]

SAFETY_SUMMARY_LINES = [
    "Routine actions (opening apps, volume, media, system info) run directly.",
    "Destructive actions may ask for a spoken yes or no first.",
    "Higher-risk actions need a confirmation on the desktop; voice alone cannot approve them.",
]


def windows_capabilities(windows_tools_enabled: bool,
                         platform: Optional[str] = None) -> List[WindowsCapability]:
    """Which Windows-control groups are usable for the given settings."""
    usable = bool(windows_tools_enabled) and (platform or sys.platform) == "win32"
    return [WindowsCapability(label, usable) for label in _WINDOWS_CAPABILITY_GROUPS]


def windows_status_lines(fast_commands_enabled: bool, windows_tools_enabled: bool,
                         platform: Optional[str] = None) -> List[str]:
    """Plain-text status rows for the Windows Control page."""
    lines = [f"Quick commands: {'On' if fast_commands_enabled else 'Off'}"]
    caps = windows_capabilities(windows_tools_enabled, platform)
    if not caps[0].available:
        reason = ("Windows only" if (platform or sys.platform) != "win32"
                  else "turned off above")
        lines.append(f"Windows control unavailable ({reason})")
    for cap in caps:
        lines.append(f"  {cap.label}: {'Available' if cap.available else 'Unavailable'}")
    return lines


# ---------------------------------------------------------------------------
# Audio device enumeration
# ---------------------------------------------------------------------------

def get_input_devices() -> List[tuple[str, str]]:
    """Return list of (value, display_name) for available audio input devices.

    Returns [("", "System Default")] if sounddevice is not available.
    """
    devices: List[tuple[str, str]] = [("", "System Default")]
    try:
        import sounddevice as sd
        from jarvis.utils.audio_lock import portaudio_lock
        with portaudio_lock:
            listed = sd.query_devices()
        for idx, dev in enumerate(listed):
            try:
                max_in = int(dev.get("max_input_channels", 0))
            except Exception:
                max_in = 0
            if max_in > 0:
                name = dev.get("name", f"Device {idx}")
                devices.append((str(idx), name))
    except Exception as e:
        debug_log(f"could not enumerate audio devices: {e}", "settings")
    return devices


# ---------------------------------------------------------------------------
# Widget builders
# ---------------------------------------------------------------------------

class SettingsWindow(QDialog):
    """Auto-generated settings UI driven by config field metadata."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Jarvis Settings")
        self.setMinimumSize(780, 560)
        self.resize(840, 620)
        self._widgets: Dict[str, Any] = {}  # key -> widget
        self._config_path = default_config_path()
        self._current_config = _load_json(self._config_path)
        # API keys still in the file move to the credential store before anything is shown.
        if _move_secrets_to_store(self._current_config):
            _save_json(self._config_path, self._current_config)
        self._defaults = get_default_config()
        extension_fields, extension_pages, extension_defaults = _extension_settings(
            {**self._defaults, **self._current_config})
        fields = _reported_model_choices(FIELD_METADATA, self._config_path.parent)
        self._fields: List[FieldMeta] = fields + extension_fields
        self._categories = CATEGORIES + extension_pages
        self._defaults = {**extension_defaults, **self._defaults}
        self._merged = {**self._defaults, **self._current_config}
        self._field_by_key = {fm.key: fm for fm in self._fields}
        self._show_advanced = self._current_config.get(SHOW_ADVANCED_KEY) is True
        # Show or hide one field's row, and the headings and labels shared by several fields.
        self._field_rows: Dict[str, Callable[[bool], None]] = {}
        self._groups: List[tuple[List[str], Callable[[bool], None]]] = []

        apply_theme(self)
        self._build_ui()
        # What each field showed on opening: Save writes only the fields the user changed, so values
        # written meanwhile by voice or the tray (reply mode, activity pause) are not reverted.
        self._initial_values = {fm.key: self._get_value(fm) for fm in self._fields if fm.field_type != "secret"}
        # The fields the last Save changed, so the app can apply the ones that take effect at once.
        self.changed_values: Dict[str, Any] = {}

    # -- UI construction ----------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        # Header
        heading, _, _ = hud_heading("Settings", "Settings",
                                    "Changes are saved to config.json. Restart Jarvis to apply.")
        layout.addWidget(heading)
        layout.addWidget(divider())

        # Sidebar + content area
        content_layout = QHBoxLayout()
        content_layout.setSpacing(16)

        # Category sidebar
        self._sidebar = QListWidget()
        self._sidebar.setObjectName("nav")
        self._sidebar.setFixedWidth(220)
        self._sidebar.setIconSize(QSize(0, 0))
        content_layout.addWidget(self._sidebar)

        # Stacked content pages
        self._pages = QStackedWidget()
        content_layout.addWidget(self._pages, 1)

        # Build pages from categories
        fields_by_cat: Dict[str, List[FieldMeta]] = {}
        for fm in self._fields:
            fields_by_cat.setdefault(fm.category, []).append(fm)

        self._page_items: Dict[str, QListWidgetItem] = {}
        for cat_key, cat_label in self._categories:
            if cat_key == "mcps":
                page = self._build_mcp_page()
            else:
                cat_fields = fields_by_cat.get(cat_key, [])
                if not cat_fields:
                    continue
                footer = None
                if cat_key == "general":
                    footer = self._build_windows_footer()
                elif cat_key == "listening":
                    footer = self._build_voice_footer()
                page = self._build_category_tab(cat_fields, footer)
                if cat_key == "general":
                    self._wire_windows_status()
            self._pages.addWidget(page)

            item = QListWidgetItem(cat_label)
            item.setSizeHint(QSize(0, 38))
            self._sidebar.addItem(item)
            self._page_items[cat_key] = item

        self._sidebar.currentRowChanged.connect(self._pages.setCurrentIndex)
        self._sidebar.setCurrentRow(0)

        layout.addLayout(content_layout, 1)

        # Button row
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)

        reset_btn = QPushButton("Reset to Defaults")
        reset_btn.setObjectName("danger")
        reset_btn.clicked.connect(self._on_reset)
        btn_layout.addWidget(reset_btn)

        self._advanced_toggle = QCheckBox("Show advanced settings")
        self._advanced_toggle.setToolTip("Show the settings most people never need to change, and the Advanced page.")
        self._advanced_toggle.setChecked(self._show_advanced)
        self._advanced_toggle.toggled.connect(self._on_show_advanced)
        btn_layout.addWidget(self._advanced_toggle)

        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = QPushButton("Save")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self._on_save)
        btn_layout.addWidget(save_btn)

        layout.addLayout(btn_layout)

        # Rows that depend on another field follow it live, before saving.
        for fm in self._fields:
            w = self._widgets[fm.key]
            if fm.field_type == "bool":
                w.toggled.connect(self._refresh_visibility)
            elif fm.field_type == "choice":
                w.currentIndexChanged.connect(self._refresh_visibility)
        self._refresh_visibility()

    def _section_heading(self, title: str) -> QLabel:
        label = QLabel(title)
        label.setObjectName("section_title")
        return label

    def _hint(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("hint")
        label.setWordWrap(True)
        return label

    def _build_category_tab(self, fields: List[FieldMeta],
                            footer: Optional[QWidget] = None) -> QWidget:
        """Build a scrollable page for a category's fields: a form under section headings, or a grid when the
        fields sit in columns. An optional informational footer spans the page width."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        container = QWidget()
        if any(fm.column for fm in fields):
            outer = QVBoxLayout(container)
            outer.setContentsMargins(16, 16, 16, 16)
            outer.addLayout(self._build_column_grid(fields))
            if footer is not None:
                outer.addWidget(footer)
            outer.addStretch(1)
            scroll.setWidget(container)
            return scroll

        form = QFormLayout(container)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)

        section = None
        section_keys: List[str] = []
        for fm in fields:
            if fm.section and fm.section != section:
                section = fm.section
                title = self._section_heading(section)
                form.addRow(title)
                section_keys = []
                self._groups.append((section_keys, lambda shown, w=title: form.setRowVisible(w, shown)))
            widget = self._create_widget(fm)
            self._widgets[fm.key] = widget

            # Label with tooltip, and the explanation under the input
            label = QLabel(fm.label)
            label.setObjectName("field_label")
            label.setToolTip(fm.description)
            label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
            cell = QWidget()
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(2)
            cell_layout.addWidget(widget)
            cell_layout.addWidget(self._hint(fm.description))

            form.addRow(label, cell)
            self._field_rows[fm.key] = lambda shown, w=cell: form.setRowVisible(w, shown)
            section_keys.append(fm.key)

        if footer is not None:
            form.addRow(footer)

        # Spacer at bottom
        form.addRow(QLabel(""), QLabel(""))

        scroll.setWidget(container)
        return scroll

    def _build_column_grid(self, fields: List[FieldMeta]) -> QGridLayout:
        """One row per setting with each column's field side by side (Codex and Claude), its explanation
        underneath and section headings between."""
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        columns = list(dict.fromkeys(fm.column for fm in fields))
        rows: Dict[tuple[str, str], List[FieldMeta]] = {}
        for fm in fields:
            rows.setdefault((fm.section, fm.label), []).append(fm)

        for index, column in enumerate(columns, start=1):
            title = QLabel(column)
            title.setObjectName("heading")
            grid.addWidget(title, 0, index)
        line = 1
        section = None
        section_keys: List[str] = []
        for (row_section, label_text), row_fields in rows.items():
            if row_section != section:
                section = row_section
                title = self._section_heading(section)
                grid.addWidget(title, line, 0, 1, len(columns) + 1)
                section_keys = []
                self._groups.append((section_keys, title.setVisible))
                line += 1
            label = QLabel(label_text)
            label.setObjectName("field_label")
            label.setToolTip(row_fields[0].description)
            grid.addWidget(label, line, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            for fm in row_fields:
                widget = self._create_widget(fm)
                self._widgets[fm.key] = widget
                grid.addWidget(widget, line, columns.index(fm.column) + 1)
                self._field_rows[fm.key] = widget.setVisible
            hint = self._hint(row_fields[0].description)
            grid.addWidget(hint, line + 1, 1, 1, len(columns))
            keys = [fm.key for fm in row_fields]
            section_keys.extend(keys)
            self._groups.append((keys, label.setVisible))
            self._groups.append((keys, hint.setVisible))
            line += 2
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        return grid

    def _create_widget(self, fm: FieldMeta) -> QWidget:
        """Create the appropriate input widget for a field."""
        current = self._merged.get(fm.key)

        if fm.field_type == "bool":
            w = QCheckBox()
            w.setChecked(bool(current))
            w.setToolTip(fm.description)
            return w

        if fm.field_type == "int":
            if fm.nullable:
                return self._create_nullable_int(fm, current)
            w = QSpinBox()
            w.setMinimum(int(fm.min_val) if fm.min_val is not None else -999999)
            w.setMaximum(int(fm.max_val) if fm.max_val is not None else 999999)
            w.setSingleStep(int(fm.step) if fm.step else 1)
            if fm.suffix:
                w.setSuffix(f" {fm.suffix}")
            try:
                w.setValue(int(current) if current is not None else 0)
            except (TypeError, ValueError):
                w.setValue(0)
            w.setToolTip(fm.description)
            return w

        if fm.field_type == "float":
            w = QDoubleSpinBox()
            w.setDecimals(_decimals(fm.step))
            w.setMinimum(fm.min_val if fm.min_val is not None else -999999.0)
            w.setMaximum(fm.max_val if fm.max_val is not None else 999999.0)
            w.setSingleStep(fm.step if fm.step else 0.1)
            if fm.suffix:
                w.setSuffix(f" {fm.suffix}")
            try:
                w.setValue(float(current) if current is not None else 0.0)
            except (TypeError, ValueError):
                w.setValue(0.0)
            w.setToolTip(fm.description)
            return w

        if fm.field_type == "choice":
            w = QComboBox()
            for val, display in (fm.choices or []):
                w.addItem(display, val)
            # Set current value
            cur_str = str(current) if current is not None else ""
            idx = w.findData(cur_str)
            if idx < 0 and cur_str:
                # A value set by hand that the list does not offer stays visible and is saved unchanged.
                w.addItem(cur_str, cur_str)
                idx = w.count() - 1
            if idx >= 0:
                w.setCurrentIndex(idx)
            w.setToolTip(fm.description)
            return w

        if fm.field_type == "device":
            w = QComboBox()
            devices = get_input_devices()
            for val, display in devices:
                w.addItem(display, val)
            cur_str = str(current) if current not in (None, "") else ""
            idx = w.findData(cur_str)
            if idx >= 0:
                w.setCurrentIndex(idx)
            w.setToolTip(fm.description)
            return w

        if fm.field_type == "list":
            return self._create_list_widget(fm, current)

        if fm.field_type == "secret":
            return self._create_secret_widget(fm)

        # Default: string field
        w = QLineEdit()
        w.setText(str(current) if current not in (None, "") else "")
        if fm.nullable:
            w.setPlaceholderText("Not set")
        w.setToolTip(fm.description)
        return w

    def _create_secret_widget(self, fm: FieldMeta) -> QWidget:
        """Whether a key is stored (never the key), a field for a new one and a Remove button.

        Changes reach the credential store on Save; nothing here is written to config.json."""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        status = QLabel()
        edit = QLineEdit()
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        remove = QPushButton("Remove")
        layout.addWidget(status)
        layout.addWidget(edit, stretch=1)
        layout.addWidget(remove)
        container._status, container._edit, container._remove = status, edit, remove
        container._stored = credentials.has_secret(fm.key)
        container._remove_pending = False

        def refresh() -> None:
            stored = container._stored and not container._remove_pending
            status.setText("Stored" if stored else ("Removed on save" if container._remove_pending
                                                   else "Not set"))
            set_role(status, "status-success" if stored else
                     "status-warning" if container._remove_pending else "status-muted")
            edit.setPlaceholderText("Enter a new key to replace it" if stored else "Enter a key to store it")
            remove.setEnabled(stored)

        def remove_key() -> None:
            container._remove_pending = True
            edit.clear()
            refresh()

        remove.clicked.connect(remove_key)
        container._refresh = refresh
        refresh()
        container.setToolTip(fm.description)
        return container

    def _apply_secret_changes(self) -> List[str]:
        """Store new keys and remove the ones marked for removal. Returns the labels that failed."""
        failed = []
        for fm in FIELD_METADATA:
            if fm.field_type != "secret":
                continue
            w = self._widgets[fm.key]
            text = w._edit.text().strip()
            if text:
                ok = credentials.set_secret(fm.key, text)
            elif w._remove_pending:
                ok = credentials.delete_secret(fm.key)
            else:
                continue
            if ok:
                w._stored, w._remove_pending = bool(text), False
                w._edit.clear()
                w._refresh()
            else:
                failed.append(fm.label)
        return failed

    def _create_nullable_int(self, fm: FieldMeta, current: Any) -> QWidget:
        """Create a combo + spinbox for an int field that can be None."""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        check = QCheckBox("Custom")
        spin = QSpinBox()
        spin.setMinimum(int(fm.min_val) if fm.min_val is not None else 0)
        spin.setMaximum(int(fm.max_val) if fm.max_val is not None else 999999)
        spin.setSingleStep(int(fm.step) if fm.step else 1)
        if fm.suffix:
            spin.setSuffix(f" {fm.suffix}")

        has_value = current is not None
        check.setChecked(has_value)
        spin.setEnabled(has_value)
        try:
            spin.setValue(int(current) if has_value else 0)
        except (TypeError, ValueError):
            spin.setValue(0)

        check.toggled.connect(spin.setEnabled)

        layout.addWidget(check)
        layout.addWidget(spin, 1)

        # Store both widgets for value extraction
        container._check = check  # type: ignore[attr-defined]
        container._spin = spin  # type: ignore[attr-defined]
        container.setToolTip(fm.description)
        return container

    def _create_list_widget(self, fm: FieldMeta, current: Any) -> QWidget:
        """Create a list editor with add/remove buttons."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        list_w = QListWidget()
        list_w.setMinimumHeight(100)
        list_w.setMaximumHeight(160)
        list_w.setToolTip(fm.description)

        # Populate with current values
        if isinstance(current, list):
            for item in current:
                if isinstance(item, str) and item.strip():
                    list_w.addItem(item.strip())

        layout.addWidget(list_w)

        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(6)

        add_btn = QPushButton("+ Add")
        edit_btn = QPushButton("Edit")
        remove_btn = QPushButton("− Remove")
        for button in (add_btn, edit_btn, remove_btn):
            button.setProperty("compact", True)
        btn_layout.addWidget(add_btn)
        btn_layout.addWidget(edit_btn)
        btn_layout.addWidget(remove_btn)
        btn_layout.addStretch()

        layout.addLayout(btn_layout)

        def _on_add():
            text, ok = QInputDialog.getText(
                self, f"Add {fm.label}",
                "Enter value (e.g. 'wrong -> right'):",
            )
            if ok and text.strip():
                list_w.addItem(text.strip())

        def _on_edit():
            item = list_w.currentItem()
            if item is None:
                return
            text, ok = QInputDialog.getText(
                self, f"Edit {fm.label}",
                "Edit value:",
                text=item.text(),
            )
            if ok and text.strip():
                item.setText(text.strip())

        def _on_remove():
            row = list_w.currentRow()
            if row >= 0:
                list_w.takeItem(row)

        add_btn.clicked.connect(_on_add)
        edit_btn.clicked.connect(_on_edit)
        remove_btn.clicked.connect(_on_remove)

        # Store the list widget for value extraction
        container._list_widget = list_w  # type: ignore[attr-defined]
        return container

    # -- MCP management page ------------------------------------------------

    def _build_windows_footer(self) -> QWidget:
        """Read-only capability status and confirmation policy summary."""
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        status_group = QGroupBox("Status")
        status_layout = QVBoxLayout(status_group)
        self._windows_status_label = QLabel()
        self._windows_status_label.setWordWrap(True)
        status_layout.addWidget(self._windows_status_label)
        layout.addWidget(status_group)

        safety_group = QGroupBox("Safety and confirmation")
        safety_layout = QVBoxLayout(safety_group)
        safety_label = QLabel("\n".join(SAFETY_SUMMARY_LINES))
        safety_label.setWordWrap(True)
        safety_layout.addWidget(safety_label)
        note = QLabel("These rules are fixed and cannot be switched off here.")
        note.setObjectName("hint")
        safety_layout.addWidget(note)
        layout.addWidget(safety_group)

        self._windows_config_note = QLabel(
            "Set in config.json, with no rows here: " + ", ".join(CONFIG_ONLY_WINDOWS_KEYS)
            + ". See " + ", ".join(CONFIG_ONLY_WINDOWS_SPECS) + "."
        )
        self._windows_config_note.setWordWrap(True)
        self._windows_config_note.setObjectName("hint")
        layout.addWidget(self._windows_config_note)
        return box

    def _build_voice_footer(self) -> QWidget:
        """Enrolment status and deletion for the stored voiceprint."""
        group = QGroupBox("Voice enrolment")
        layout = QVBoxLayout(group)
        self._voice_status_label = QLabel()
        self._voice_status_label.setWordWrap(True)
        layout.addWidget(self._voice_status_label)
        self._voice_hint_label = QLabel(
            "Enrol by running  python scripts/enrol_voice.py  and reading the prompts aloud. "
            "The voiceprint stays on this PC next to config.json and is never sent anywhere."
        )
        self._voice_hint_label.setWordWrap(True)
        self._voice_hint_label.setObjectName("hint")
        layout.addWidget(self._voice_hint_label)
        self._delete_voiceprint_button = QPushButton("Delete my voiceprint")
        self._delete_voiceprint_button.setObjectName("danger")
        self._delete_voiceprint_button.clicked.connect(self._delete_voiceprint)
        layout.addWidget(self._delete_voiceprint_button, 0, Qt.AlignmentFlag.AlignLeft)
        self._refresh_voice_status()
        return group

    def _refresh_voice_status(self) -> None:
        from jarvis.listening import voiceprint

        enrolled = voiceprint.has_voiceprint()
        self._voice_status_label.setText("Voice enrolled" if enrolled else "Voice not enrolled")
        set_role(self._voice_status_label, "status-success" if enrolled else "status-muted")
        self._delete_voiceprint_button.setEnabled(enrolled)

    def _delete_voiceprint(self) -> None:
        from jarvis.listening import voiceprint

        reply = QMessageBox.question(
            self, "Delete voiceprint",
            "Delete your enrolled voiceprint? Speaker verification stops working until you enrol again.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            voiceprint.delete_voiceprint()
        self._refresh_voice_status()

    def _wire_windows_status(self) -> None:
        """Keep the status panel in step with the two toggles."""
        for key in ("fast_commands_enabled", "windows_tools_enabled"):
            self._widgets[key].toggled.connect(self._refresh_windows_status)
        self._refresh_windows_status()

    def _refresh_windows_status(self, *_args) -> None:
        self._windows_status_label.setText("\n".join(windows_status_lines(
            self._widgets["fast_commands_enabled"].isChecked(),
            self._widgets["windows_tools_enabled"].isChecked(),
        )))

    # -- Visibility: advanced settings and rows that depend on other fields --

    def _on_show_advanced(self, shown: bool) -> None:
        self._show_advanced = bool(shown)
        self._refresh_visibility()

    def _current(self, key: str) -> Any:
        """A field's value as the window shows it now, or the saved value for a key without a row."""
        fm = self._field_by_key.get(key)
        if fm is not None and key in self._widgets:
            return self._get_value(fm)
        return self._merged.get(key)

    def _field_shown(self, fm: FieldMeta) -> bool:
        if fm.advanced and not self._show_advanced:
            return False
        return fm.shown_when is None or bool(fm.shown_when(self._current))

    def _refresh_visibility(self, *_args) -> None:
        shown = {fm.key: self._field_shown(fm) for fm in self._fields}
        for key, show in self._field_rows.items():
            show(shown.get(key, True))
        for keys, show in self._groups:
            show(any(shown.get(key, True) for key in keys))
        advanced_item = self._page_items.get(ADVANCED_CATEGORY)
        if advanced_item is not None:
            advanced_item.setHidden(not self._show_advanced)
            if advanced_item.isHidden() and self._sidebar.currentItem() is advanced_item:
                self._sidebar.setCurrentRow(0)

    def is_field_shown(self, key: str) -> bool:
        """Whether the field's row is shown on its page with the window's current values."""
        fm = self._field_by_key.get(key)
        return fm is not None and key in self._widgets and self._field_shown(fm)

    def visible_pages(self) -> List[str]:
        """The sidebar entries currently listed, in order."""
        items = (self._sidebar.item(row) for row in range(self._sidebar.count()))
        return [item.text() for item in items if not item.isHidden()]

    def set_show_advanced(self, shown: bool) -> None:
        self._advanced_toggle.setChecked(shown)

    def grid_row(self, key: str) -> int:
        """The grid row a field in a column page sits on."""
        widget = self._widgets[key]
        grid = widget.parentWidget().layout()
        for index in range(grid.count()):
            item = grid.itemAt(index)
            if isinstance(item.layout(), QGridLayout):
                grid = item.layout()
                break
        row, _column, _rows, _columns = grid.getItemPosition(grid.indexOf(widget))
        return row

    def _build_mcp_page(self) -> QWidget:
        """Build the MCP servers management page."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Header
        desc = QLabel(
            "MCP (Model Context Protocol) servers give Jarvis extra tools: "
            "file access, web search, databases, and more."
        )
        desc.setWordWrap(True)
        desc.setObjectName("body")
        layout.addWidget(desc)

        # Server list
        self._mcp_list = QListWidget()
        self._mcp_list.setMinimumHeight(180)
        self._mcp_list.setMaximumHeight(300)
        layout.addWidget(self._mcp_list)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(6)

        add_catalogue_btn = QPushButton("Add from Catalogue")
        add_catalogue_btn.setToolTip("Pick from a list of popular MCP servers")
        add_catalogue_btn.clicked.connect(self._on_mcp_add_catalogue)
        btn_layout.addWidget(add_catalogue_btn)

        add_custom_btn = QPushButton("+ Add Custom")
        add_custom_btn.setToolTip("Manually configure an MCP server")
        add_custom_btn.clicked.connect(self._on_mcp_add_custom)
        btn_layout.addWidget(add_custom_btn)

        edit_btn = QPushButton("Edit")
        edit_btn.clicked.connect(self._on_mcp_edit)
        btn_layout.addWidget(edit_btn)

        remove_btn = QPushButton("− Remove")
        remove_btn.clicked.connect(self._on_mcp_remove)
        btn_layout.addWidget(remove_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # Details panel for selected server
        self._mcp_detail = QLabel("")
        self._mcp_detail.setWordWrap(True)
        self._mcp_detail.setObjectName("detail_panel")
        self._mcp_detail.setMinimumHeight(60)
        layout.addWidget(self._mcp_detail)

        self._mcp_list.currentRowChanged.connect(self._on_mcp_selection_changed)

        # Populate from current config
        self._mcp_configs: Dict[str, Dict] = dict(self._merged.get("mcps", {}) or {})
        self._refresh_mcp_list()

        layout.addStretch()
        scroll.setWidget(container)
        return scroll

    def _refresh_mcp_list(self) -> None:
        """Refresh the MCP server list widget from the in-memory dict."""
        self._mcp_list.clear()
        for name, cfg in self._mcp_configs.items():
            catalogue_entry = CATALOGUE_BY_NAME.get(name)
            if catalogue_entry:
                display = f"{catalogue_entry.display_name}  ({name})"
            else:
                display = name
            self._mcp_list.addItem(display)
        if self._mcp_list.count() == 0:
            self._mcp_detail.setText("No MCP servers configured. Add one to extend Jarvis's capabilities.")
        else:
            self._mcp_list.setCurrentRow(0)

    def _on_mcp_selection_changed(self, row: int) -> None:
        """Update the detail panel when an MCP server is selected."""
        if row < 0 or row >= len(self._mcp_configs):
            self._mcp_detail.setText("")
            return
        name = list(self._mcp_configs.keys())[row]
        cfg = self._mcp_configs[name]
        command = cfg.get("command", "")
        args = " ".join(str(a) for a in cfg.get("args", []))
        env_keys = ", ".join(cfg.get("env", {}).keys()) if cfg.get("env") else "none"
        self._mcp_detail.setText(
            f"<b>Name:</b> {name}<br>"
            f"<b>Command:</b> {command}<br>"
            f"<b>Args:</b> {args}<br>"
            f"<b>Env vars:</b> {env_keys}"
        )

    def _on_mcp_add_catalogue(self) -> None:
        """Show a dialog to pick from the curated catalogue."""
        dlg = _MCPCatalogueDialog(self._mcp_configs, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            for entry, extra_env in dlg.selected_entries_with_env():
                self._mcp_configs[entry.name] = entry.to_config(extra_env=extra_env)
            self._refresh_mcp_list()

    def _on_mcp_add_custom(self) -> None:
        """Show a dialog to manually add an MCP server."""
        dlg = _MCPEditDialog(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            name, cfg = dlg.get_result()
            if name:
                self._mcp_configs[name] = cfg
                self._refresh_mcp_list()

    def _on_mcp_edit(self) -> None:
        """Edit the selected MCP server."""
        row = self._mcp_list.currentRow()
        if row < 0:
            return
        name = list(self._mcp_configs.keys())[row]
        cfg = self._mcp_configs[name]
        dlg = _MCPEditDialog(name=name, config=cfg, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_name, new_cfg = dlg.get_result()
            if new_name:
                if new_name != name:
                    del self._mcp_configs[name]
                self._mcp_configs[new_name] = new_cfg
                self._refresh_mcp_list()

    def _on_mcp_remove(self) -> None:
        """Remove the selected MCP server."""
        row = self._mcp_list.currentRow()
        if row < 0:
            return
        name = list(self._mcp_configs.keys())[row]
        reply = QMessageBox.question(
            self, "Remove MCP Server",
            f"Remove '{name}'?\n\nYou can always re-add it later.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            del self._mcp_configs[name]
            self._refresh_mcp_list()

    # -- Value extraction ---------------------------------------------------

    def _get_value(self, fm: FieldMeta) -> Any:
        """Extract the current value from a widget."""
        w = self._widgets[fm.key]

        if fm.field_type == "bool":
            return w.isChecked()

        if fm.field_type == "int" and fm.nullable:
            if hasattr(w, '_check') and not w._check.isChecked():
                return None
            return w._spin.value()

        if fm.field_type == "int":
            return w.value()

        if fm.field_type == "float":
            return round(w.value(), 3)

        if fm.field_type in ("choice", "device"):
            val = w.currentData()
            # For sample_rate, convert back to int
            if fm.key == "sample_rate":
                try:
                    return int(val)
                except (TypeError, ValueError):
                    return 16000
            if val == "" and fm.field_type == "choice" and any(value == "" for value, _ in (fm.choices or [])):
                return ""  # an offered empty choice ("Model default", "Automatic") is a value of its own
            return val if val != "" else None

        if fm.field_type == "list":
            list_w = w._list_widget
            return [list_w.item(i).text() for i in range(list_w.count())]

        if fm.field_type == "secret":
            return None

        # str
        text = w.text().strip()
        if fm.nullable and text == "":
            return None
        return text

    # -- Actions ------------------------------------------------------------

    def _on_save(self) -> None:
        """Collect values from widgets and save to config.json."""
        # A required text field left blank (the wake word) is refused before anything is written.
        blank = [fm for fm in self._fields if fm.required and fm.field_type == "str"
                 and not self._widgets[fm.key].text().strip()]
        if blank:
            item = self._page_items.get(blank[0].category)
            if item is not None:
                self._sidebar.setCurrentItem(item)
            debug_log(f"settings not saved: {blank[0].key} is blank", "settings")
            QMessageBox.warning(self, "Settings not saved",
                                f"{blank[0].label} cannot be empty. Enter a value, then save again.")
            return

        # Start from the file as it is now (keys the UI does not show, and values written while the
        # window was open by voice or the tray), and apply only the fields the user changed.
        config = _load_json(self._config_path) or {}
        self.changed_values = {}
        failed_secrets = self._apply_secret_changes()

        for fm in self._fields:
            if fm.field_type == "secret":
                # API keys never go into config.json (see _apply_secret_changes); one that could not
                # move to the store stays where it is rather than being lost.
                credentials.drop_stored_plaintext(config, fm.key)
                continue
            val = self._get_value(fm)
            if fm.key in self._initial_values and val == self._initial_values[fm.key]:
                continue
            self.changed_values[fm.key] = val
            default_val = self._defaults.get(fm.key)

            # Only write non-default values to keep config.json clean.
            if _is_default_value(val, default_val):
                config.pop(fm.key, None)
            else:
                config[fm.key] = val

        if self._show_advanced:
            config[SHOW_ADVANCED_KEY] = True
        else:
            config.pop(SHOW_ADVANCED_KEY, None)

        # Save MCP configs (empty dict = no MCPs, omit from config)
        if self._mcp_configs:
            config["mcps"] = dict(self._mcp_configs)
        else:
            config.pop("mcps", None)

        if failed_secrets:
            QMessageBox.warning(
                self, "Credential store",
                "These keys could not be saved to Windows Credential Manager, so they were not saved:\n"
                + "\n".join(failed_secrets)
            )

        if _save_json(self._config_path, config):
            debug_log("settings saved to config.json", "settings")
            QMessageBox.information(
                self, "Settings saved",
                "Settings saved. Restart Jarvis for changes to take effect."
            )
            self.accept()
        else:
            QMessageBox.warning(
                self, "Settings not saved",
                f"Could not save settings to:\n{self._config_path}"
            )

    def _on_reset(self) -> None:
        """Reset all fields to defaults."""
        reply = QMessageBox.question(
            self, "Reset to Defaults",
            "Reset all settings to their default values?\n\n"
            "This will overwrite your config.json.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._merged = dict(self._defaults)
        self._current_config = {}

        # Refresh all widgets
        for fm in self._fields:
            self._set_widget_value(fm, self._defaults.get(fm.key))

        # Clear MCP configs
        self._mcp_configs = {}
        self._refresh_mcp_list()

        debug_log("settings reset to defaults", "settings")

    def _set_widget_value(self, fm: FieldMeta, value: Any) -> None:
        """Set a widget's value from a config value."""
        w = self._widgets.get(fm.key)
        if w is None:
            return

        if fm.field_type == "bool":
            w.setChecked(bool(value))

        elif fm.field_type == "int" and fm.nullable:
            has_val = value is not None
            w._check.setChecked(has_val)
            w._spin.setEnabled(has_val)
            try:
                w._spin.setValue(int(value) if has_val else 0)
            except (TypeError, ValueError):
                w._spin.setValue(0)

        elif fm.field_type == "int":
            try:
                w.setValue(int(value) if value is not None else 0)
            except (TypeError, ValueError):
                w.setValue(0)

        elif fm.field_type == "float":
            try:
                w.setValue(float(value) if value is not None else 0.0)
            except (TypeError, ValueError):
                w.setValue(0.0)

        elif fm.field_type in ("choice", "device"):
            cur_str = str(value) if value not in (None, "") else ""
            idx = w.findData(cur_str)
            if idx >= 0:
                w.setCurrentIndex(idx)

        elif fm.field_type == "secret":
            # Reset clears typed input only; stored keys are removed with Remove.
            w._edit.clear()
            w._remove_pending = False
            w._refresh()

        elif fm.field_type == "list":
            list_w = w._list_widget
            list_w.clear()
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        list_w.addItem(item.strip())

        else:  # str
            w.setText(str(value) if value not in (None, "") else "")


# ---------------------------------------------------------------------------
# MCP dialogue windows
# ---------------------------------------------------------------------------

class _MCPCatalogueDialog(QDialog):
    """Dialog for picking MCP servers from the curated catalogue."""

    def __init__(self, existing: Dict[str, Dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("MCP Server Catalogue")
        self.setMinimumSize(480, 420)
        apply_theme(self)

        self._existing = existing
        self._checkboxes: Dict[str, QCheckBox] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        heading, _, _ = hud_heading(
            "MCP", "Server Catalogue", "Select MCP servers to add. Already-configured servers are shown as checked.")
        layout.addWidget(heading)
        layout.addWidget(divider())

        # Node.js availability warning
        node_warning = QLabel(
            "<b>Node.js not found.</b> Most MCP servers require Node.js. "
            f"{link('https://nodejs.org/', 'Download Node.js')} "
            "and restart Jarvis to use them."
        )
        node_warning.setOpenExternalLinks(True)
        node_warning.setWordWrap(True)
        node_warning.setObjectName("notice_error")
        node_warning.setVisible(not self._is_node_available())
        layout.addWidget(node_warning)

        # Scrollable list of catalogue entries
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setSpacing(8)

        for entry in CATALOGUE:
            card = QFrame()
            card.setObjectName("card")
            card_layout = QHBoxLayout(card)
            card_layout.setContentsMargins(0, 0, 0, 0)
            card_layout.setSpacing(12)

            cb = QCheckBox()
            already_added = entry.name in existing
            cb.setChecked(already_added)
            if already_added:
                cb.setEnabled(False)
                cb.setToolTip("Already configured")
            self._checkboxes[entry.name] = cb
            card_layout.addWidget(cb)

            text_layout = QVBoxLayout()
            text_layout.setSpacing(2)

            name_label = QLabel(entry.display_name)
            name_label.setObjectName("heading")
            text_layout.addWidget(name_label)

            desc_label = QLabel(entry.description)
            desc_label.setWordWrap(True)
            desc_label.setObjectName("muted")
            text_layout.addWidget(desc_label)

            if entry.needs_api_key:
                key_label = QLabel(f"Requires {entry.api_key_env_var}")
                key_label.setObjectName("accent_hint")
                text_layout.addWidget(key_label)

            card_layout.addLayout(text_layout, 1)
            inner_layout.addWidget(card)

        inner_layout.addStretch()
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        add_btn = QPushButton("Add Selected")
        add_btn.setObjectName("primary")
        add_btn.clicked.connect(self._on_add)
        btn_layout.addWidget(add_btn)
        layout.addLayout(btn_layout)

    def _on_add(self) -> None:
        """Prompt for API keys if needed, then accept."""
        self._collected_env: Dict[str, Dict[str, str]] = {}
        for entry in self._selected_new_entries():
            if entry.needs_api_key and entry.api_key_env_var:
                key, ok = QInputDialog.getText(
                    self,
                    f"{entry.display_name} API Key",
                    f"Enter your {entry.api_key_env_var}:\n"
                    f"({entry.api_key_hint or ''})",
                )
                if ok and key.strip():
                    self._collected_env[entry.name] = {entry.api_key_env_var: key.strip()}
                else:
                    # User cancelled key entry: skip this entry
                    self._checkboxes[entry.name].setChecked(False)
                    continue
        self.accept()

    @staticmethod
    def _is_node_available() -> bool:
        """Check if Node.js (npx) is available on the system."""
        try:
            from jarvis.tools.external.mcp_client import _resolve_command
            _resolve_command("npx")
            return True
        except (FileNotFoundError, Exception):
            return False

    def _selected_new_entries(self) -> List[MCPEntry]:
        """Return catalogue entries the user selected (excluding already-configured)."""
        result = []
        for name, cb in self._checkboxes.items():
            if cb.isChecked() and cb.isEnabled():
                result.append(CATALOGUE_BY_NAME[name])
        return result

    def selected_entries_with_env(self) -> List[tuple]:
        """Return list of (MCPEntry, extra_env_dict) for each selected entry."""
        collected = getattr(self, "_collected_env", {})
        return [
            (entry, collected.get(entry.name, {}))
            for entry in self._selected_new_entries()
        ]


class _MCPEditDialog(QDialog):
    """Dialog for adding or editing a single MCP server configuration."""

    def __init__(self, name: str = "", config: Optional[Dict] = None, parent=None):
        super().__init__(parent)
        self._is_edit = bool(name)
        self.setWindowTitle("Edit MCP Server" if self._is_edit else "Add Custom MCP Server")
        self.setMinimumSize(440, 340)
        apply_theme(self)

        config = config or {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        heading, _, _ = hud_heading("MCP", "Edit Server" if self._is_edit else "Add Custom Server")
        layout.addWidget(heading)
        layout.addWidget(divider())

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self._name_edit = QLineEdit(name)
        self._name_edit.setPlaceholderText("e.g. filesystem, my-server")
        if self._is_edit:
            self._name_edit.setEnabled(False)
        form.addRow("Name", self._name_edit)

        self._command_edit = QLineEdit(str(config.get("command", "")))
        self._command_edit.setPlaceholderText("e.g. npx, node, python")
        form.addRow("Command", self._command_edit)

        self._args_edit = QLineEdit(" ".join(str(a) for a in config.get("args", [])))
        self._args_edit.setPlaceholderText("e.g. -y @modelcontextprotocol/server-filesystem ~")
        self._args_edit.setToolTip("Space-separated arguments")
        form.addRow("Args", self._args_edit)

        env = config.get("env") or {}
        env_str = " ".join(f"{k}={v}" for k, v in env.items())
        self._env_edit = QLineEdit(env_str)
        self._env_edit.setPlaceholderText("e.g. API_KEY=abc123 (space-separated KEY=VALUE)")
        form.addRow("Env vars", self._env_edit)

        layout.addLayout(form)
        layout.addStretch()

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        save_btn = QPushButton("Save")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self._on_save)
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

    def _on_save(self) -> None:
        name = self._name_edit.text().strip()
        command = self._command_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Missing Name", "Please enter a server name.")
            return
        if not command:
            QMessageBox.warning(self, "Missing Command", "Please enter a command.")
            return
        self.accept()

    def get_result(self) -> tuple:
        """Return (name, config_dict) from the dialog fields."""
        name = self._name_edit.text().strip()
        command = self._command_edit.text().strip()
        args_text = self._args_edit.text().strip()
        args = args_text.split() if args_text else []
        env_text = self._env_edit.text().strip()
        env = {}
        if env_text:
            for pair in env_text.split():
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    env[k] = v

        cfg = {"transport": "stdio", "command": command, "args": args}
        if env:
            cfg["env"] = env
        return name, cfg
