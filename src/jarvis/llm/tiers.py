"""The model tiers.

Jarvis runs every LLM context on one of two models, or a third when the opt-in tool model is set:

- ``Tier.FAST`` — the small, warm, low-latency model behind real-time
  classification passes. These contexts take a few thousand tokens in and
  emit tiny strict-JSON answers, so latency dominates and a ~2B model is
  ideal.
- ``Tier.CHAT`` — the capable model that writes replies, plans,
  summarises, and extracts knowledge. Long-form output; quality dominates.
- ``Tier.TOOL`` — the opt-in model that chooses and calls tools in a local
  reply's tool phase; empty when tool-model mode is off.
- ``Tier.BACKGROUND`` — chat-quality local work that runs whatever the
  reply mode (diary, graph extraction, meal logging, dictation clean-up):
  the chat model while the local model writes replies, the fast model while
  Codex or Claude does, so a cloud session never pages in the chat model.

The Model tiers table in ``llm.spec.md`` is the authoritative list of
which context runs on which tier; docstrings elsewhere point here rather
than re-enumerating it.

Both fields are fully resolved at config load (``fast_model`` /
``llm_chat_model`` always hold a provider-valid model name), so resolution
here is a plain field read. Keeping it behind one function means every
context states its tier instead of inventing a fallback chain, and any
future routing logic lands in exactly one place.
"""

from __future__ import annotations

from enum import Enum


class Tier(Enum):
    """Which model a context runs on."""

    FAST = "fast"
    CHAT = "chat"
    TOOL = "tool"
    BACKGROUND = "background"


def resolve_model(cfg, tier: Tier) -> str:
    """Return the model name for ``tier`` under the active settings.

    ``Tier.FAST`` reads ``cfg.fast_model``; ``Tier.CHAT`` reads
    ``cfg.llm_chat_model``. An empty fast model (possible only on
    hand-built cfg objects — config load always resolves it) falls back
    to the chat model so a context never receives an empty name.
    ``Tier.TOOL`` reads ``cfg.tool_model`` and is empty while tool-model
    mode is off; it never falls back. ``Tier.BACKGROUND`` resolves as
    ``Tier.CHAT`` in local reply mode and as ``Tier.FAST`` otherwise.
    """
    if tier is Tier.TOOL:
        tool = getattr(cfg, "tool_model", "")
        return tool.strip() if isinstance(tool, str) else ""
    if tier is Tier.BACKGROUND:
        from ..bridge import modes
        return resolve_model(cfg, Tier.CHAT if modes.active_mode() == modes.LOCAL else Tier.FAST)
    chat = str(getattr(cfg, "llm_chat_model", "") or "").strip()
    if tier is Tier.FAST:
        return str(getattr(cfg, "fast_model", "") or "").strip() or chat
    return chat


def local_reply_models(cfg) -> list:
    """Models only local replies use: the chat and tool models, less any the fast tier or embeddings share."""
    shared = {resolve_model(cfg, Tier.FAST), str(getattr(cfg, "embedding_model", "") or "").strip()}
    models = []
    for model in (resolve_model(cfg, Tier.CHAT), resolve_model(cfg, Tier.TOOL)):
        if model and model not in shared and model not in models:
            models.append(model)
    return models
