"""How `configure` asks questions.

Kept behind a `Protocol` so `survey.py` and `reconcile.py` never call
`input()` directly — a scripted double drives every test in this package
without a pseudo-tty, and the same double will later drive `--yes`
(accept-every-default) non-interactive runs by scripting nothing but
returning every default.
"""

from typing import Protocol


class Prompter(Protocol):
    def confirm(self, question: str, *, default: bool) -> bool: ...
    def text(self, question: str, *, default: str = "") -> str: ...


class StdinPrompter:
    """Reads real answers from stdin. `[Y/n]`/`[y/N]` mirrors the default."""

    def confirm(self, question: str, *, default: bool) -> bool:
        hint = "[Y/n]" if default else "[y/N]"
        answer = input(f"{question} {hint} ").strip().lower()
        if not answer:
            return default
        return answer in ("y", "yes")

    def text(self, question: str, *, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        answer = input(f"{question}{suffix} ").strip()
        return answer or default


class ScriptedPrompter:
    """Replays canned answers in call order. Falls back to each call's own
    `default` once its script is exhausted, rather than raising — a test
    that only cares about the first N prompts should not have to script
    every prompt after them."""

    def __init__(self, *, confirms: list[bool], texts: list[str]) -> None:
        self._confirms = list(confirms)
        self._texts = list(texts)

    def confirm(self, question: str, *, default: bool) -> bool:
        if self._confirms:
            return self._confirms.pop(0)
        return default

    def text(self, question: str, *, default: str = "") -> str:
        if self._texts:
            return self._texts.pop(0)
        return default
