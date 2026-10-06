"""Argument validation against a tool's JSON input schema."""
from typing import Any, Optional


def validate_arguments(schema: Optional[dict], args: Optional[Any]) -> Optional[str]:
    """Return a short error string when ``args`` don't satisfy ``schema``.

    Lightweight check limited to the failure modes that matter for model-chosen
    calls: non-object arguments, unknown argument keys and missing required
    keys. Type-checking is left to the tools, because a stricter pre-check
    would reject too many borderline cases. Returns ``None`` when the arguments
    pass or when no schema is available.
    """
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return "arguments is not an object"
    if not schema:
        return None
    props = schema.get("properties")
    if not isinstance(props, dict):
        return None
    allowed_keys = set(props.keys())
    unknown = [k for k in args.keys() if k not in allowed_keys]
    if unknown:
        expected = sorted(allowed_keys) or ["(none)"]
        return (
            f"unknown argument key(s) {sorted(unknown)!r}; "
            f"expected one of {expected!r}"
        )
    required = schema.get("required")
    if isinstance(required, list):
        missing = [r for r in required if isinstance(r, str) and r not in args]
        if missing:
            return f"missing required argument(s) {sorted(missing)!r}"
    return None
