"""The opt-in tool model is chosen on the LLM page, off by default."""
import pytest

from desktop_app.settings_window import FIELD_METADATA


@pytest.mark.unit
def test_the_tool_model_is_an_llm_choice_that_defaults_to_off():
    field = next(f for f in FIELD_METADATA if f.key == "tool_model")
    assert field.category == "llm" and field.field_type == "choice"
    values = [value for value, _ in field.choices]
    assert values[0] == "" and "gpt-oss:20b" in values
