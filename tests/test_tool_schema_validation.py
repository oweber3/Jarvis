"""Pure argument validation against a tool's JSON input schema."""
import pytest

from jarvis.tools.schema_validation import validate_arguments

SCHEMA = {
    "type": "object",
    "properties": {"action": {"type": "string"}, "percent": {"type": "number"}},
    "required": ["action"],
}


@pytest.mark.unit
class TestValidateArguments:
    def test_valid_arguments_pass(self):
        assert validate_arguments(SCHEMA, {"action": "set", "percent": 30}) is None

    def test_missing_required_key_is_reported(self):
        assert "missing required" in validate_arguments(SCHEMA, {"percent": 3})

    def test_unknown_key_is_reported(self):
        assert "unknown argument" in validate_arguments(SCHEMA, {"action": "set", "bogus": 1})

    def test_non_object_arguments_are_rejected(self):
        assert validate_arguments(SCHEMA, ["action"]) is not None

    def test_none_means_no_arguments(self):
        assert "missing required" in validate_arguments(SCHEMA, None)
        assert validate_arguments({"type": "object", "properties": {}}, None) is None

    def test_missing_schema_accepts_anything(self):
        assert validate_arguments(None, {"x": 1}) is None
        assert validate_arguments({}, {"x": 1}) is None
