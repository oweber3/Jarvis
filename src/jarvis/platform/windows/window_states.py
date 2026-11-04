"""One spelling for window states and actions: the British forms the tool schemas list.

Models and callers often write the American spelling; ``normalise_window_word`` maps ``maximize`` and
``minimize`` (and the British words), in any case, to ``maximise`` and ``minimise`` before validation.
Pure Python, so it imports on any platform."""
from typing import Any

_BRITISH = {'maximise': 'maximise', 'maximize': 'maximise', 'minimise': 'minimise', 'minimize': 'minimise'}


def normalise_window_word(value: Any) -> Any:
    """``maximise`` or ``minimise`` for either spelling in any case; any other value unchanged."""
    if not isinstance(value, str):
        return value
    return _BRITISH.get(value.strip().casefold(), value)
