"""Include/exclude matching for S3 keys.

Patterns are shell-style globs (:mod:`fnmatch`), which makes them predictable
in a way that the substring matching used before 2.0 was not -- ``raw`` no
longer silently excludes ``brawl/``.

Matching rules
--------------
A pattern containing ``/`` is matched against the **whole key**::

    logs/*.gz          -> logs/a.gz            (not logs/2024/a.gz)
    logs/**            -> anything under logs/
    2023/              -> anything under any path segment chain "2023/"

A pattern with no ``/`` is matched against the **basename** and against every
**directory component**::

    .DS_Store          -> any file named .DS_Store, at any depth
    *.tmp              -> any file ending in .tmp
    .@__thumb          -> anything inside a directory named exactly .@__thumb
    @eaDir*            -> anything inside a directory starting with @eaDir

``**`` crosses ``/`` boundaries; a single ``*`` does not.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence

__all__ = ["KeyFilter", "load_pattern_file", "compile_pattern"]


def _translate(pattern: str) -> str:
    """Translate a glob to a regex, giving ``**`` cross-directory meaning.

    :func:`fnmatch.translate` maps ``*`` to ``.*``, which would happily cross
    ``/``. We tokenise first so a single ``*`` stops at a separator.
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            if pattern.startswith("**", i):
                # Consume the whole run of '*' so '***' behaves like '**'.
                while i < n and pattern[i] == "*":
                    i += 1
                out.append(".*")
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = i + 1
            if end < n and pattern[end] in ("!", "^"):
                end += 1
            if end < n and pattern[end] == "]":
                end += 1
            while end < n and pattern[end] != "]":
                end += 1
            if end >= n:
                out.append(re.escape("["))
            else:
                body = pattern[i + 1 : end].replace("\\", r"\\")
                if body[0] in ("!", "^"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = end + 1
                continue
        else:
            out.append(re.escape(char))
        i += 1
    return "".join(out)


def compile_pattern(pattern: str, ignore_case: bool = False) -> CompiledPattern:
    """Compile one glob into a matcher. Raises ``ValueError`` if unusable."""
    pattern = pattern.strip()
    if not pattern:
        raise ValueError("empty pattern")

    # A trailing slash means "this directory and everything under it".
    directory_only = pattern.endswith("/")
    if directory_only:
        pattern = pattern.rstrip("/")
        if not pattern:
            raise ValueError("pattern is only slashes")

    scoped = "/" in pattern
    body = _translate(pattern)
    if directory_only:
        regex = f"(?:.*/)?{body}/.*" if not scoped else f"{body}/.*"
    elif scoped:
        # Anchored at the start of the key, matching how S3 prefixes read.
        regex = body
    else:
        # Basename match, or any intermediate directory component.
        regex = f"(?:.*/)?{body}(?:/.*)?"

    flags = re.IGNORECASE if ignore_case else 0
    try:
        compiled = re.compile(f"(?s:{regex})\\Z", flags)
    except re.error as exc:  # pragma: no cover - defensive
        raise ValueError(f"invalid pattern {pattern!r}: {exc}") from exc
    return CompiledPattern(pattern, compiled)


class CompiledPattern:
    """A single compiled glob, retaining its source text for log messages."""

    __slots__ = ("source", "_regex")

    def __init__(self, source: str, regex: re.Pattern[str]) -> None:
        self.source = source
        self._regex = regex

    def matches(self, key: str) -> bool:
        return self._regex.match(key) is not None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CompiledPattern({self.source!r})"


class KeyFilter:
    """Decides whether a key is in scope.

    A key is in scope when it matches at least one include pattern (or no
    include patterns were supplied) and no exclude pattern. Excludes always
    win, matching the behaviour of ``rsync`` and ``.gitignore``.
    """

    def __init__(
        self,
        excludes: Sequence[str] | None = None,
        includes: Sequence[str] | None = None,
        ignore_case: bool = False,
    ) -> None:
        self.excludes = [compile_pattern(p, ignore_case) for p in (excludes or [])]
        self.includes = [compile_pattern(p, ignore_case) for p in (includes or [])]

    def __bool__(self) -> bool:
        return bool(self.excludes or self.includes)

    def match(self, key: str) -> tuple[bool, str]:
        """Return ``(keep, reason)``. ``reason`` is set only when dropping."""
        for pattern in self.excludes:
            if pattern.matches(key):
                return False, f"excluded by {pattern.source!r}"
        if self.includes:
            for pattern in self.includes:
                if pattern.matches(key):
                    return True, ""
            return False, "did not match any --include pattern"
        return True, ""

    def keeps(self, key: str) -> bool:
        return self.match(key)[0]

    def describe(self) -> str:
        parts = []
        if self.includes:
            parts.append("include=" + ", ".join(p.source for p in self.includes))
        if self.excludes:
            parts.append("exclude=" + ", ".join(p.source for p in self.excludes))
        return "; ".join(parts) if parts else "none"


def load_pattern_file(path: str) -> list[str]:
    """Read patterns from a file, one per line.

    Blank lines and ``#`` comments are ignored. Raises ``FileNotFoundError``
    if the file is missing -- a typo'd skip-list silently restoring everything
    is exactly the failure this tool must not have.
    """
    patterns: list[str] = []
    with open(os.path.expanduser(path), encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
    return patterns


def validate_patterns(patterns: Iterable[str]) -> list[str]:
    """Return the list of patterns that fail to compile."""
    bad = []
    for pattern in patterns:
        try:
            compile_pattern(pattern)
        except ValueError:
            bad.append(pattern)
    return bad
