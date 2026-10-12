"""Tests for recall_gate.should_recall — cheap heuristic for skipping
long-term memory enrichment when the hot-window already covers the topic.
"""

import pytest

from src.jarvis.memory.recall_gate import should_recall


@pytest.mark.unit
class TestShouldRecall:
    def test_empty_hot_window_always_recalls(self):
        assert should_recall("who is justin bieber", []) is True

    def test_no_tool_result_in_history_always_recalls(self):
        recent = [
            {"role": "user", "content": "who is justin bieber"},
            {"role": "assistant", "content": "He is a Canadian singer."},
        ]
        # No tool row → no fresh grounded data → recall (diary may know more)
        assert should_recall("what is his most famous song", recent) is True

    def test_tool_covered_topic_skips_recall(self):
        recent = [
            {"role": "user", "content": "who is justin bieber"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "webSearch",
                              "arguments": {"query": "justin bieber"}}}
            ]},
            {"role": "tool", "tool_call_id": "c1",
             "content": "Justin Bieber is a Canadian singer with hits like Baby."},
            {"role": "assistant", "content": "Canadian singer."},
        ]
        # Follow-up on same entity, fresh tool row present → skip
        assert should_recall("what is his most famous song bieber hits",
                             recent) is False

    def test_topic_change_still_recalls(self):
        recent = [
            {"role": "user", "content": "who is justin bieber"},
            {"role": "tool", "tool_call_id": "c1",
             "content": "Justin Bieber is a Canadian singer."},
            {"role": "assistant", "content": "Canadian singer."},
        ]
        # Completely different topic → no overlap → recall runs
        assert should_recall("what's the weather in hackney", recent) is True

    def test_non_latin_script_query_matches_hot_window(self):
        """Per CLAUDE.md the gate must be language-agnostic. A Cyrillic query
        covered by a Cyrillic tool result should skip recall just like English.
        """
        recent = [
            {"role": "user", "content": "кто такой пушкин"},
            {"role": "tool", "tool_call_id": "c1",
             "content": "Пушкин это русский поэт девятнадцатого века."},
            {"role": "assistant", "content": "Русский поэт."},
        ]
        assert should_recall("пушкин русский поэт стихи", recent) is False

    def test_non_latin_topic_change_still_recalls(self):
        recent = [
            {"role": "user", "content": "кто такой пушкин"},
            {"role": "tool", "tool_call_id": "c1",
             "content": "Пушкин это русский поэт."},
        ]
        # Different topic in the same script → no overlap → recall
        assert should_recall("какая сегодня погода", recent) is True

    def test_stopword_only_query_does_not_skip(self):
        recent = [
            {"role": "tool", "tool_call_id": "c1", "content": "foo bar"},
        ]
        # "what is it" has no content words → cannot justify skipping
        assert should_recall("what is it", recent) is True


@pytest.mark.unit
def test_skip_log_carries_counts_never_query_words(monkeypatch):
    """Query words are split into pieces no redaction can recognise (an email address becomes its
    name and domain parts), so the skip log line carries counts only."""
    from src.jarvis.memory import recall_gate

    logged = []
    monkeypatch.setattr(recall_gate, "debug_log", lambda msg, tag=None: logged.append(msg))
    query = "send the report to priya.sharma@example.org"
    recent = [
        {"role": "user", "content": query},
        {"role": "tool", "content": "report for priya.sharma@example.org ready to send"},
    ]

    assert recall_gate.should_recall(query, recent) is False
    assert logged
    for word in ("priya", "sharma", "example", "report"):
        assert all(word not in line for line in logged), logged
