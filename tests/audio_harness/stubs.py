"""Stand-ins for the listener's collaborators, for tests that do not need real models."""

from __future__ import annotations

from typing import Callable, Optional, Union

from jarvis.listening.speaker_verification import Verdict


class StubVerifier:
    """A speaker verifier that answers from a script instead of a model.

    ``verdicts`` are handed out one per ``check`` call (then ``default``);
    ``score`` is a number, or a callable ``audio -> float``, for barge-in's
    short-window comparison.
    """

    def __init__(self, *verdicts: Verdict, default: Verdict = Verdict.OWNER,
                 score: Union[float, Callable] = 0.9) -> None:
        self.verdicts = list(verdicts)
        self.default = default
        self._score = score
        self.calls = 0
        self.score_calls = 0
        self.windows: list[float] = []

    def check(self, audio, **_kwargs) -> Verdict:
        self.calls += 1
        return self.verdicts.pop(0) if self.verdicts else self.default

    def allows(self, audio, **kwargs) -> bool:
        return self.check(audio, **kwargs) is not Verdict.OTHER

    def score(self, audio, min_seconds: Optional[float] = None) -> Optional[float]:
        self.score_calls += 1
        self.windows.append(len(audio) / 16000.0)
        return self._score(audio) if callable(self._score) else float(self._score)
