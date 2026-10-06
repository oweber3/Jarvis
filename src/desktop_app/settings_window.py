"""
Jarvis Settings Window

Auto-generated settings UI driven by config metadata.
Reads/writes config.json directly and groups settings by category.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QWidget,
    QLabel, QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox,
    QComboBox, QScrollArea, QGroupBox, QFormLayout, QPushButton,
    QMessageBox, QSizePolicy, QListWidget, QListWidgetItem,
    QStackedWidget, QSplitter, QInputDialog, QFrame,
)
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QFont

from jarvis import credentials
from jarvis.config import (
    get_default_config, load_config,
    default_config_path, _save_json, _load_json, _move_secrets_to_store,
    SUPPORTED_CHAT_MODELS,
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


# Categories and their display order
CATEGORIES = [
    ("reply", "Reply Mode"),
    ("codex", "Codex (optional)"),
    ("claude", "Claude (optional)"),
    ("llm", "LLM & AI Models"),
    ("llm_provider", "LLM Provider"),
    ("tts", "Text-to-Speech"),
    ("piper", "Piper TTS"),
    ("chatterbox", "Chatterbox TTS"),
    ("voice_input", "Voice Input"),
    ("wake", "Wake Word"),
    ("whisper", "Speech Recognition"),
    ("vad", "Voice Activity Detection"),
    ("timing", "Timing & Windows"),
    ("memory", "Memory & Dialogue"),
    ("location", "Location"),
    ("windows", "Windows Control"),
    ("activity", "Activity Log (optional)"),
    ("remote", "Phone Access (optional)"),
    ("features", "Features"),
    ("mcps", "MCP Servers"),
    ("advanced", "Advanced"),
]

# Windows features configured only in config.json (no rows), named on the Windows Control page.
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

# Models the Codex and Claude runtimes list for a subscription sign-in, as their model-list calls name them.
# The bridge preflight checks the chosen one against the live list, and an unlisted value set by hand is
# shown as an extra entry, so these lists only need to cover the usual picks.
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
    ("sonnet", "Sonnet"),
    ("haiku", "Haiku (fastest, no effort levels)"),
    ("opus", "Opus"),
    ("claude-fable-5-1[1m]", "Fable"),
    ("default", "Claude Code default"),
]


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


def _build_field_metadata() -> List[FieldMeta]:
    """Build the metadata registry for all user-facing config fields."""
    fields = []

    def f(key, label, desc, cat, ftype, **kw):
        fields.append(FieldMeta(key=key, label=label, description=desc,
                                category=cat, field_type=ftype, **kw))

    # --- LLM & AI Models ---
    model_choices = [(mid, info["name"]) for mid, info in SUPPORTED_CHAT_MODELS.items()]
    # GPT-OSS 20B is offered for every role so the tool model can also write replies without a second
    # model loading on each request (reply.spec.md, Tool-Model Mode).
    gpt_oss = ("gpt-oss:20b", "GPT-OSS 20B (best tool use, ~13GB)")
    # Granite 4.2 8B is the light alternative measured for tool-model mode (docs/TOOL_MODEL_BENCHMARK.md).
    granite = ("granite4.2:8b", "Granite 4.2 8B (light tool use, ~5GB)")
    f("ollama_chat_model", "Chat Model", "Primary LLM for conversations",
      "llm", "choice", choices=model_choices + [gpt_oss, granite])
    f("ollama_embed_model", "Embedding Model", "Model for text embeddings",
      "llm", "str")
    f("ollama_base_url", "Ollama URL", "Ollama server base URL",
      "llm", "str")
    f("llm_chat_timeout_sec", "Chat Timeout", "Max seconds for chat responses",
      "llm", "float", min_val=10, max_val=600, step=10, suffix="s")
    f("llm_num_ctx", "Ollama Context Size", "Stable context window shared by Ollama inference requests",
      "llm", "int", min_val=512, max_val=262144, step=1024)
    f("llm_keep_alive", "Model Residency",
      "How long Ollama keeps a model loaded after a request: a duration such as 30m or 2h, or -1 to keep "
      "it loaded. Longer avoids reload delays and holds memory. Low-power mode uses 1m.",
      "llm", "str")
    f("llm_routing_timeout_sec", "Routing Timeout", "Max seconds for LLM routing and startup probes",
      "llm", "float", min_val=1, max_val=600, step=1, suffix="s")
    f("llm_tools_timeout_sec", "Tools Timeout", "Max seconds for tool calls",
      "llm", "float", min_val=10, max_val=600, step=10, suffix="s")
    f("llm_embedding_timeout_sec", "Embedding Timeout", "Max seconds for embeddings",
      "llm", "float", min_val=5, max_val=300, step=5, suffix="s")
    f("fast_model", "Fast Model",
      "Small, quick model for real-time work: voice intent, tool routing, "
      "quick classifications. Automatic picks the right default for your provider",
      "llm", "choice", choices=[("", "Automatic (recommended)")] + model_choices + [gpt_oss, granite])
    f("tool_model", "Tool Model",
      "Optional separate model that chooses and calls tools while the chat model writes the reply. "
      "GPT-OSS 20B chose tools best in testing but needs about 13 GB, so the two models load in turn "
      "unless both fit in your GPU memory. Off lets the chat model do both",
      "llm", "choice",
      choices=[("", "Off (chat model uses tools)"), gpt_oss, granite] + model_choices)
    f("intent_judge_timeout_sec", "Intent Judge Timeout",
      "Max seconds for intent judgement",
      "llm", "float", min_val=1, max_val=30, step=0.5, suffix="s")
    f("llm_thinking_enabled", "Chat Thinking Mode",
      "Let the chat model think/reason before answering (slower but may improve quality)",
      "llm", "bool")
    f("intent_judge_thinking_enabled", "Intent Judge Thinking Mode",
      "Let the intent judge think before classifying (adds latency to wake detection)",
      "llm", "bool")

    # --- LLM Provider ---
    # Selects which local runtime serves the LLM. The connection and model
    # fields below are nullable: leaving them empty falls back to the Ollama
    # settings on the "LLM & AI Models" page, so a default (Ollama) install
    # never needs to touch this page.
    f("llm_provider", "Provider", "Which local runtime serves the LLM",
      "llm_provider", "choice",
      choices=[("ollama", "Ollama (local)"),
               ("openai_compatible", "OpenAI-compatible server")])
    f("llm_base_url", "Base URL",
      "Provider API base URL (e.g. http://localhost:1234/v1 for LM Studio). "
      "Leave empty to use the Ollama URL.",
      "llm_provider", "str", nullable=True)
    f("llm_api_key", "API Key",
      "Bearer token for the provider, if it requires one. Kept in Windows Credential Manager, never in "
      "config.json; only whether one is stored is shown.",
      "llm_provider", "secret")
    f("llm_chat_model", "Chat Model",
      "Model name the provider exposes. Leave empty to use the Ollama chat model.",
      "llm_provider", "str", nullable=True)
    f("embedding_provider", "Embedding Provider",
      "Runtime for embeddings. Leave on 'Same as chat provider' unless your "
      "chat runtime has no embeddings endpoint (then route them to Ollama).",
      "llm_provider", "choice",
      choices=[("", "Same as chat provider"),
               ("ollama", "Ollama (local)"),
               ("openai_compatible", "OpenAI-compatible server")])
    f("embedding_base_url", "Embedding Base URL",
      "Override base URL for embeddings. Leave empty to inherit from the "
      "chat provider (or the Ollama URL).",
      "llm_provider", "str", nullable=True)
    f("embedding_api_key", "Embedding API Key",
      "Override bearer token for embeddings; without one the chat key is used. Kept in Windows Credential "
      "Manager, never in config.json; only whether one is stored is shown.",
      "llm_provider", "secret")
    f("embedding_model", "Embedding Model",
      "Embedding model name. Leave empty to use the Ollama embedding model.",
      "llm_provider", "str", nullable=True)

    # --- Text-to-Speech ---
    f("tts_enabled", "Enable TTS", "Enable text-to-speech output",
      "tts", "bool")
    f("tts_pc_speakers_enabled", "Voice on This PC",
      "Play Jarvis's voice through this PC's speakers or headphones. Turning it off while an extension's "
      "voice output is on makes Jarvis speak only through that output.",
      "tts", "bool")
    f("tts_engine", "TTS Engine", "Speech synthesis engine",
      "tts", "choice", choices=[("piper", "Piper (Neural)"), ("chatterbox", "Chatterbox (Voice Cloning)")])
    f("tts_rate", "Speech Rate", "Words per minute (200 = normal)",
      "tts", "int", min_val=80, max_val=400, step=10, suffix="WPM", nullable=True)

    # --- Piper TTS ---
    f("tts_piper_length_scale", "Speed Scale",
      "Speech speed: <1.0 faster, >1.0 slower",
      "piper", "float", min_val=0.1, max_val=3.0, step=0.05)
    f("tts_piper_noise_scale", "Audio Variation",
      "Higher = more expressive",
      "piper", "float", min_val=0.0, max_val=2.0, step=0.05)
    f("tts_piper_noise_w", "Phoneme Width Variation",
      "Higher = more lively rhythm",
      "piper", "float", min_val=0.0, max_val=2.0, step=0.05)
    f("tts_piper_sentence_silence", "Sentence Silence",
      "Pause after each sentence",
      "piper", "float", min_val=0.0, max_val=2.0, step=0.05, suffix="s")
    f("tts_piper_model_path", "Custom Voice Model",
      "Path to .onnx voice model (leave empty for default)",
      "piper", "str", nullable=True)
    f("tts_piper_speaker", "Speaker ID",
      "Speaker index for multi-speaker models",
      "piper", "int", min_val=0, max_val=99, nullable=True)

    # --- Chatterbox TTS ---
    f("tts_chatterbox_device", "Device",
      "Compute device for Chatterbox",
      "chatterbox", "choice",
      choices=[("cuda", "CUDA (GPU)"), ("auto", "Auto"), ("cpu", "CPU")])
    f("tts_chatterbox_exaggeration", "Exaggeration",
      "Emotion exaggeration (0.0–1.0+)",
      "chatterbox", "float", min_val=0.0, max_val=2.0, step=0.05)
    f("tts_chatterbox_cfg_weight", "CFG Weight",
      "Quality/speed trade-off",
      "chatterbox", "float", min_val=0.0, max_val=2.0, step=0.05)
    f("tts_chatterbox_audio_prompt", "Voice Clone Audio",
      "Path to audio file for voice cloning (leave empty to disable)",
      "chatterbox", "str", nullable=True)

    # --- Voice Input ---
    f("voice_device", "Input Device",
      "Microphone device (name or index). Leave empty for system default.",
      "voice_input", "device")
    f("sample_rate", "Sample Rate",
      "Audio sample rate in Hz",
      "voice_input", "choice",
      choices=[("16000", "16000 Hz"), ("44100", "44100 Hz"), ("48000", "48000 Hz")])
    f("voice_min_energy", "Min Energy",
      "Minimum audio energy to register voice",
      "voice_input", "float", min_val=0.0, max_val=1.0, step=0.005)
    f("speaker_verification", "Speaker Verification",
      "Only respond to your own voice. Soft ignores voices that are confidently someone else; "
      "strict needs your voice. Requires an enrolled voice (see below).",
      "voice_input", "choice",
      choices=[("off", "Off"), ("soft", "Soft: ignore other voices"), ("strict", "Strict: owner only")])
    f("barge_in_enabled", "Talk Over Jarvis",
      "Lower Jarvis's voice as soon as your enrolled voice is heard over it (needs speaker verification)",
      "voice_input", "bool")

    # --- Wake Word ---
    f("wake_word", "Wake Word",
      "Primary wake word to activate Jarvis",
      "wake", "str")
    f("wake_fuzzy_ratio", "Fuzzy Match Ratio",
      "How loosely to match the wake word (0.0–1.0)",
      "wake", "float", min_val=0.5, max_val=1.0, step=0.01)
    # --- Whisper ---
    f("whisper_model", "Model Size",
      "Whisper model size (tiny/base/small/medium/large)",
      "whisper", "choice",
      choices=[("tiny", "Tiny"), ("base", "Base"), ("small", "Small"),
               ("medium", "Medium"), ("large-v3", "Large v3")])
    # MLX runs only on Apple Silicon; a value set by hand elsewhere still shows (see the choice widget).
    whisper_backend_choices = [("auto", "Auto")]
    if sys.platform == "darwin":
        whisper_backend_choices.append(("mlx", "MLX (Apple Silicon)"))
    whisper_backend_choices.append(("faster-whisper", "Faster Whisper"))
    f("whisper_backend", "Backend",
      "Speech recognition backend",
      "whisper", "choice",
      choices=whisper_backend_choices)
    f("whisper_device", "Compute Device",
      "Device for Whisper inference",
      "whisper", "choice",
      choices=[("auto", "Auto"), ("cuda", "CUDA (GPU)"), ("cpu", "CPU")])
    f("whisper_compute_type", "Compute Type",
      "Quantisation level for inference",
      "whisper", "choice",
      choices=[("int8", "INT8 (Fast)"), ("float16", "Float16"), ("float32", "Float32")])
    f("whisper_vad", "Use VAD Filter",
      "Filter audio with VAD before transcription",
      "whisper", "bool")
    f("whisper_min_confidence", "Min Confidence",
      "Filter low-confidence segments (hallucination guard)",
      "whisper", "float", min_val=0.0, max_val=1.0, step=0.05)
    f("whisper_no_speech_threshold", "No-Speech Threshold",
      "Reject segments where no_speech_prob is at or above this value (filters hallucinations during silence)",
      "whisper", "float", min_val=0.0, max_val=1.0, step=0.05)

    # --- VAD ---
    f("vad_enabled", "Enable VAD",
      "Use Voice Activity Detection",
      "vad", "bool")
    f("vad_aggressiveness", "Aggressiveness",
      "VAD aggressiveness (0=least, 3=most aggressive)",
      "vad", "int", min_val=0, max_val=3)
    f("endpoint_silence_ms", "Endpoint Silence",
      "Silence duration to end an utterance",
      "vad", "int", min_val=100, max_val=5000, step=50, suffix="ms")
    f("max_utterance_ms", "Max Utterance",
      "Maximum single utterance duration",
      "vad", "int", min_val=1000, max_val=60000, step=1000, suffix="ms")
    f("tts_max_utterance_ms", "Max Utterance (During TTS)",
      "Shorter timeout during TTS for quick stop detection",
      "vad", "int", min_val=500, max_val=10000, step=500, suffix="ms")

    # --- Timing & Windows ---
    f("voice_collect_seconds", "Collect Window",
      "Silence after you stop speaking before the request is sent",
      "timing", "float", min_val=0.5, max_val=30.0, step=0.1, suffix="s")
    f("voice_wake_wait_seconds", "Wake Word Wait",
      "Silence allowed after the wake word alone before the request starts",
      "timing", "float", min_val=1.0, max_val=30.0, step=0.5, suffix="s")
    f("voice_max_collect_seconds", "Max Collect Window",
      "Maximum time to collect continuous speech",
      "timing", "float", min_val=10.0, max_val=600.0, step=10, suffix="s")
    f("hot_window_enabled", "Hot Window",
      "Enable follow-up window after responses",
      "timing", "bool")
    f("hot_window_seconds", "Hot Window Duration",
      "Duration of follow-up window",
      "timing", "float", min_val=1.0, max_val=30.0, step=0.5, suffix="s")
    f("transcript_buffer_duration_sec", "Transcript Buffer",
      "Duration of rolling transcript history for intent judging",
      "timing", "float", min_val=10, max_val=600, step=10, suffix="s")

    # --- Memory & Dialogue ---
    f("dialogue_memory_timeout", "Memory & Diary Window",
      "Duration for dialogue memory and forced diary updates",
      "memory", "float", min_val=30, max_val=3600, step=30, suffix="s")
    f("memory_enrichment_max_results", "Enrichment Results",
      "Max memory results for context enrichment",
      "memory", "int", min_val=1, max_val=50)
    f("memory_enrichment_source", "Enrichment Source",
      "Which memory system enriches replies: all (diary + graph), diary only, or graph only",
      "memory", "choice", choices=[("diary", "Diary only"), ("graph", "Graph only"), ("all", "All (diary + graph)")])
    f("tool_carryover_max_turns", "Tool Carryover Turns",
      "How many prior replies' tool results to keep visible for follow-up questions",
      "memory", "int", min_val=0, max_val=10)
    f("tool_carryover_per_entry_chars", "Tool Carryover Length",
      "Chars kept per carried-over tool result (UNTRUSTED fence markers preserved)",
      "memory", "int", min_val=200, max_val=8000, step=100)
    f("agentic_max_turns", "Agentic Max Turns",
      "Maximum turns in agentic tool-use loops",
      "memory", "int", min_val=1, max_val=30)

    # --- Location ---
    f("location_enabled", "Enable Location",
      "Allow location-aware responses",
      "location", "bool")
    f("location_auto_detect", "Auto-Detect",
      "Automatically detect location from IP",
      "location", "bool")
    f("location_cache_minutes", "Cache Duration",
      "Minutes to cache location data",
      "location", "int", min_val=1, max_val=1440, step=5, suffix="min")
    f("location_ip_address", "IP Address Override",
      "Manual IP for geolocation (leave empty for auto)",
      "location", "str", nullable=True)
    f("location_cgnat_resolve_public_ip", "CGNAT Resolve",
      "Resolve public IP when behind CGNAT",
      "location", "bool")

    # --- Windows Control ---
    f("fast_commands_enabled", "Quick Commands",
      "Run simple, unambiguous commands such as \"open Word\", \"set volume to 30%\" "
      "or \"what time is it\" instantly, without waiting for the AI model. "
      "Anything unclear still goes to the AI.",
      "windows", "bool")
    f("windows_tools_enabled", "Windows Control Tools",
      "Enable native Windows tools (applications, windows, paths, volume, media and system information)",
      "windows", "bool")
    f("screen_awareness_enabled", "Screen Awareness",
      "Let Jarvis look at your screen when you ask about it (\"what's on my screen?\", \"what does this "
      "error mean?\"). It captures the screen only for that request, reads its text on this PC and never "
      "stores it. In Codex or Claude mode the screen you asked about is sent to that model. Turn this off "
      "and Jarvis never looks at the screen. Takes effect after Jarvis restarts.",
      "windows", "bool")
    f("windows_fancyzones_enabled", "FancyZones Zones",
      "Let window placement use the zone layouts you set up in Microsoft PowerToys FancyZones. "
      "Jarvis only reads them from this PC and never changes them.",
      "windows", "bool")
    f("roku_host", "TV Address (Roku)",
      "The Roku TV's address on your home network, such as 192.168.1.50, to control it by voice. Empty turns TV "
      "control off. Only private home-network addresses are accepted. Reserve the address for the TV in your "
      "router so it does not change, and turn on Settings > System > Advanced system settings > Control by "
      "mobile apps > Network access (Default or Permissive) on the TV. To find the address, run "
      "python -m jarvis.devices.roku --discover. Restart Jarvis after changing it.",
      "windows", "str", nullable=True)

    # --- Reply mode: who answers, and which cloud modes may be switched to ---
    f("reply_mode", "Reply Mode",
      "Who answers when Jarvis starts. Local keeps everything on this PC and works offline. Switch at any "
      "time from the tray or by saying \"use Claude\", \"use ChatGPT\" or \"go local\"; a cloud mode must "
      "also be allowed below, otherwise Jarvis stays local.",
      "reply", "choice",
      choices=[("local", "Local (offline, default)"), ("codex", "ChatGPT through Codex (cloud)"),
               ("claude", "Claude through Claude Code (cloud)")])
    f("codex_enabled", "Allow Codex Mode",
      "Lets Jarvis switch to Codex. In that mode a hidden Codex process interprets requests and uses "
      "Jarvis's tools: the request, the context chosen on the Codex page and tool results are sent to "
      "OpenAI using your Codex ChatGPT sign-in. Off means nothing can switch Jarvis to Codex.",
      "reply", "bool")
    f("claude_enabled", "Allow Claude Mode",
      "Lets Jarvis switch to Claude. In that mode a hidden Claude Code process interprets requests and uses "
      "Jarvis's tools: the request, the context chosen on the Claude page and tool results are sent to "
      "Anthropic using your Claude subscription sign-in (never an API key). Off means nothing can switch "
      "Jarvis to Claude.",
      "reply", "bool")

    # --- Codex (optional reply mode) ---
    f("codex_model", "Codex Model",
      "The model Codex mode answers with. Jarvis checks that your Codex sign-in offers it and never "
      "substitutes another model. A model ID set by hand in config.json is shown and kept.",
      "codex", "choice", choices=CODEX_MODEL_CHOICES)
    f("codex_reasoning_effort", "Reasoning Effort",
      "Lower is faster. Jarvis checks that the model supports it and never substitutes another value.",
      "codex", "choice",
      choices=[("low", "Low (fastest)"), ("medium", "Medium"), ("high", "High"),
               ("xhigh", "Extra high"), ("max", "Maximum"), ("ultra", "Ultra")])
    f("codex_executable", "Codex Executable",
      "Leave as codex to use the Codex command line on PATH or the copy installed with the Codex app, "
      "or enter the full path to codex.exe.",
      "codex", "str")
    f("codex_timeout_sec", "Answer Deadline",
      "Seconds to wait for Codex to finish a request.",
      "codex", "float", min_val=5, max_val=600, step=5, suffix="s")
    f("codex_queue_limit", "Queued Requests",
      "Requests allowed to wait behind the one in progress. More are refused as busy.",
      "codex", "int", min_val=0, max_val=5, step=1)
    f("codex_max_tool_calls", "Tool Calls Per Request",
      "Most tool calls Codex may make for one request.",
      "codex", "int", min_val=1, max_val=20, step=1)
    f("codex_share_recent_dialogue", "Share Recent Conversation",
      "Send the last few messages so follow-ups make sense. They are redacted and bounded. When off, "
      "every request starts with no earlier conversation.",
      "codex", "bool")
    f("codex_recent_dialogue_messages", "Messages Shared",
      "How many recent messages are shared when conversation sharing is on.",
      "codex", "int", min_val=0, max_val=20, step=1)
    f("codex_share_desktop_referents", "Share Recent Desktop Actions",
      "Send Jarvis's own record of what it acted on in this conversation: the last few windows it opened, "
      "focused or placed (application, window handle, display, zone, state), the device it controlled, the "
      "media player and what kind of thing is on the clipboard, so \"move it\" or \"pause it\" acts on the "
      "right thing. No window titles, paths, track names, clipboard contents or conversation text. When off, "
      "follow-ups need the name of what they are about.",
      "codex", "bool")
    f("codex_share_foreground_window", "Share The Window You're Looking At",
      "With each request you make at the PC, send which window is in front (application, window handle, "
      "display, state), so \"close this\" acts on it. Never its title, and never for requests from your phone. "
      "When off, Codex has to ask or look the window up with a tool.",
      "codex", "bool")
    f("codex_share_long_term_memory", "Share Personal Data Tools",
      "Off by default: nothing from your diary, memory graph or meal log is ever disclosed. Turn on to "
      "let Codex use the tools that read them.",
      "codex", "bool")

    # --- Claude (optional reply mode) ---
    f("claude_model", "Claude Model",
      "The model Claude mode answers with. Jarvis checks that Claude Code offers it and never substitutes "
      "another model. A model set by hand in config.json is shown and kept.",
      "claude", "choice", choices=CLAUDE_MODEL_CHOICES)
    f("claude_effort", "Effort",
      "Lower is faster. Jarvis checks that the model supports it and never substitutes another value. "
      "Models without effort levels ignore it.",
      "claude", "choice",
      choices=[("low", "Low (fastest)"), ("medium", "Medium"), ("high", "High"), ("xhigh", "Extra high"),
               ("max", "Maximum"), ("", "Model default")])
    f("claude_executable", "Claude Executable",
      "Leave as claude to use Claude Code on PATH or the copy its installer put in your user folder, or "
      "enter the full path to claude.exe.",
      "claude", "str")
    f("claude_timeout_sec", "Answer Deadline",
      "Seconds to wait for Claude to finish a request.",
      "claude", "float", min_val=5, max_val=600, step=5, suffix="s")
    f("claude_queue_limit", "Queued Requests",
      "Requests allowed to wait behind the one in progress. More are refused as busy.",
      "claude", "int", min_val=0, max_val=5, step=1)
    f("claude_max_tool_calls", "Tool Calls Per Request",
      "Most tool calls Claude may make for one request.",
      "claude", "int", min_val=1, max_val=20, step=1)
    f("claude_share_recent_dialogue", "Share Recent Conversation",
      "Send the last few messages so follow-ups make sense. They are redacted and bounded. When off, "
      "every request starts with no earlier conversation.",
      "claude", "bool")
    f("claude_recent_dialogue_messages", "Messages Shared",
      "How many recent messages are shared when conversation sharing is on.",
      "claude", "int", min_val=0, max_val=20, step=1)
    f("claude_share_desktop_referents", "Share Recent Desktop Actions",
      "Send Jarvis's own record of what it acted on in this conversation: the last few windows it opened, "
      "focused or placed (application, window handle, display, zone, state), the device it controlled, the "
      "media player and what kind of thing is on the clipboard, so \"move it\" or \"pause it\" acts on the "
      "right thing. No window titles, paths, track names, clipboard contents or conversation text. When off, "
      "follow-ups need the name of what they are about.",
      "claude", "bool")
    f("claude_share_foreground_window", "Share The Window You're Looking At",
      "With each request you make at the PC, send which window is in front (application, window handle, "
      "display, state), so \"close this\" acts on it. Never its title, and never for requests from your phone. "
      "When off, Claude has to ask or look the window up with a tool.",
      "claude", "bool")
    f("claude_share_long_term_memory", "Share Personal Data Tools",
      "Off by default: nothing from your diary, memory graph or meal log is ever disclosed. Turn on to "
      "let Claude use the tools that read them.",
      "claude", "bool")

    # --- Features ---
    f("web_search_enabled", "Web Search",
      "Enable web search tool",
      "features", "bool")
    f("brave_search_api_key", "Brave Search API Key",
      "Optional. When set, Brave is used as the primary fallback if DuckDuckGo "
      "is blocked. Free tier: 2,000 queries/month at api.search.brave.com. Kept in Windows Credential "
      "Manager, never in config.json; only whether one is stored is shown.",
      "features", "secret")
    f("wikipedia_fallback_enabled", "Wikipedia Fallback",
      "Use Wikipedia as a last-resort source when other search engines fail. "
      "No key, no account, privacy-light.",
      "features", "bool")
    f("low_power_mode", "Low Power Mode",
      "Reduce background LLM residency and skip LLM startup warmup",
      "features", "bool")
    f("wake_overlay_enabled", "Wake Screen Effect",
      "Fill the edges of every screen with soft light that breathes and pulses while Jarvis is awake, "
      "from the wake word until the conversation ends. Click-through and never takes focus; screens showing a "
      "full-screen app are left alone.",
      "features", "bool")
    f("dictation_enabled", "Dictation Mode",
      "Hold a hotkey to record speech, release to paste transcription into any app",
      "features", "bool")
    f("dictation_hotkey", "Dictation Hotkey",
      "Key combination to hold for dictation. Double-tap for hands-free mode.",
      "features", "choice", choices=_dictation_hotkey_choices())
    f("dictation_filler_removal", "Filler Word Removal",
      "Use the local LLM to remove filler words (um, uh, like) from dictation output",
      "features", "bool")
    f("dictation_thinking_enabled", "Dictation Thinking Mode",
      "Let the LLM think when cleaning dictation (adds latency after each dictation)",
      "features", "bool")
    f("dictation_custom_dictionary", "Custom Dictionary",
      "Correction rules for dictation. Use 'wrong -> right' format (e.g. 'Jarvice -> Jarvis')",
      "features", "list")

    # --- Activity log (optional, off by default) ---
    f("activity_log_enabled", "Record Activity",
      "Off by default. When on, Jarvis records which application is in the foreground and its window "
      "title (with emails, tokens and similar secrets removed), and when you are idle, so you can ask "
      "what you were working on. It is stored only in Jarvis's database on this PC. It never records "
      "keystrokes, screen contents, files, or password managers and private browsing windows. Pause or "
      "delete it from the tray at any time. Takes effect after Jarvis restarts.",
      "activity", "bool")
    f("activity_log_paused", "Paused",
      "Stop recording without turning the log off. Also available from the tray.",
      "activity", "bool")
    f("activity_log_retention_days", "Keep History",
      "Days of history to keep. Older sessions are deleted once a day.",
      "activity", "int", min_val=1, max_val=365, step=1, suffix="days")
    f("activity_log_idle_after_sec", "Idle After",
      "Seconds without keyboard or mouse input before time is recorded as idle instead of as the "
      "application you left open.",
      "activity", "float", min_val=30, max_val=3600, step=30, suffix="s")
    f("activity_log_excluded_processes", "Never Record These Applications",
      "Executable names that are never recorded (not even that they were used). Defaults to common "
      "password managers. Replacing this list replaces the defaults.",
      "activity", "list")
    f("activity_log_private_title_markers", "Never Record Windows Containing",
      "Window titles containing any of these words are never recorded. Defaults to the private-browsing "
      "markers of common browsers in many languages.",
      "activity", "list")
    f("activity_log_share_with_cloud", "Share With Codex And Claude",
      "Off by default: the activity log is available only to the local model, and never leaves this PC. "
      "Turn on to let the Codex and Claude reply modes read it, which sends the parts they ask for "
      "(application names and window titles) to OpenAI or Anthropic.",
      "activity", "bool")

    # --- Phone access (opt-in) ---
    f("remote_access_enabled", "Phone Access",
      "Reach Jarvis from your phone: the orb, the conversation and desktop confirmations in your phone's "
      "browser. Jarvis serves the page itself on this PC and answers only phones you pair, on your home "
      "network or your own VPN (such as Tailscale). Pair a phone from the tray: Phone Access. "
      "Restart Jarvis after changing it.",
      "remote", "bool")
    f("remote_access_host", "Listen Address",
      "0.0.0.0 listens on every network of this PC; only private addresses are ever answered. Enter one "
      "address, such as this PC's Tailscale address, to answer on that network only.",
      "remote", "str")
    f("remote_access_port", "Port",
      "The port the phone connects to, as in http://<this PC>:8765/.",
      "remote", "int", min_val=1024, max_val=65535, step=1)
    f("remote_access_allow_confirm", "Approve From The Phone",
      "Let a paired phone approve or deny actions that ask for confirmation on the desktop, such as "
      "deleting a file. Off keeps those answers on this PC.",
      "remote", "bool")
    f("remote_access_quick_actions", "Quick Actions",
      "Commands shown as one-tap buttons on the phone, one per line. Each is sent as if typed.",
      "remote", "list")

    # --- Advanced ---
    f("echo_energy_threshold", "Echo Energy Threshold",
      "Threshold for echo detection",
      "advanced", "float", min_val=0.0, max_val=10.0, step=0.1)
    f("echo_tolerance", "Echo Tolerance",
      "Time tolerance for echo detection",
      "advanced", "float", min_val=0.0, max_val=2.0, step=0.05, suffix="s")

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
        for idx, dev in enumerate(sd.query_devices()):
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
        self._fields: List[FieldMeta] = FIELD_METADATA + extension_fields
        self._categories = CATEGORIES + extension_pages
        self._defaults = {**extension_defaults, **self._defaults}
        self._merged = {**self._defaults, **self._current_config}

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

        for cat_key, cat_label in self._categories:
            if cat_key == "mcps":
                page = self._build_mcp_page()
            else:
                cat_fields = fields_by_cat.get(cat_key, [])
                if not cat_fields:
                    continue
                footer = None
                if cat_key == "windows":
                    footer = self._build_windows_footer()
                elif cat_key == "voice_input":
                    footer = self._build_voice_footer()
                page = self._build_category_tab(cat_fields, footer)
                if cat_key == "windows":
                    self._wire_windows_status()
            self._pages.addWidget(page)

            item = QListWidgetItem(cat_label)
            item.setSizeHint(QSize(0, 38))
            self._sidebar.addItem(item)

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

        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = QPushButton("Save")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self._on_save)
        btn_layout.addWidget(save_btn)

        layout.addLayout(btn_layout)

    def _build_category_tab(self, fields: List[FieldMeta],
                            footer: Optional[QWidget] = None) -> QWidget:
        """Build a scrollable form for a category's fields, with an optional
        informational footer spanning the form width."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        container = QWidget()
        form = QFormLayout(container)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(14)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        for fm in fields:
            widget = self._create_widget(fm)
            self._widgets[fm.key] = widget

            # Label with tooltip
            label = QLabel(fm.label)
            label.setObjectName("field_label")
            label.setToolTip(fm.description)
            label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

            form.addRow(label, widget)

        if footer is not None:
            form.addRow(footer)

        # Spacer at bottom
        form.addRow(QLabel(""), QLabel(""))

        scroll.setWidget(container)
        return scroll

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
            w.setDecimals(3)
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
            w.setPlaceholderText("Leave empty for default")
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
        group = QGroupBox("Your voice")
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
