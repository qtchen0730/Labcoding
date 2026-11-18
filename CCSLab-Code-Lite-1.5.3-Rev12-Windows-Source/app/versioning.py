"""Single-source application version metadata and strict SemVer ordering."""

from functools import total_ordering
import json
from pathlib import Path
import re
import sys


_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class VersionError(ValueError):
    pass


@total_ordering
class SemVer:
    def __init__(self, value):
        if not isinstance(value, str):
            raise VersionError("Version must be a string")
        match = _SEMVER.fullmatch(value)
        if not match:
            raise VersionError("Invalid SemVer version")
        self.value = value
        self.core = tuple(int(match.group(i)) for i in range(1, 4))
        self.prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
        self.build = match.group(5) or ""

    @property
    def stable(self):
        return not self.prerelease

    def _compare(self, other):
        other = other if isinstance(other, SemVer) else SemVer(other)
        if self.core != other.core:
            return (self.core > other.core) - (self.core < other.core)
        if not self.prerelease or not other.prerelease:
            return (not self.prerelease) - (not other.prerelease)
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            left_num, right_num = left.isdigit(), right.isdigit()
            if left_num and right_num:
                return (int(left) > int(right)) - (int(left) < int(right))
            if left_num != right_num:
                return -1 if left_num else 1
            return (left > right) - (left < right)
        return (len(self.prerelease) > len(other.prerelease)) - (
            len(self.prerelease) < len(other.prerelease))

    def __eq__(self, other):
        try:
            return self._compare(other) == 0
        except (TypeError, VersionError):
            return NotImplemented

    def __lt__(self, other):
        return self._compare(other) < 0

    def __str__(self):
        return self.value

    def __repr__(self):
        return f"SemVer({self.value!r})"


def read_version_info(path=None):
    if path is None:
        if getattr(sys, "frozen", False):
            path = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "version.json"
        else:
            path = Path(__file__).with_name("version.json")
    try:
        info = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VersionError("Version metadata is unavailable") from error
    if not isinstance(info, dict):
        raise VersionError("Version metadata must be an object")
    version = SemVer(info.get("version"))
    sequence = info.get("release_sequence")
    channel = info.get("channel")
    if type(sequence) is not int or sequence < 1:
        raise VersionError("Invalid release sequence")
    if channel not in {"stable", "beta"}:
        raise VersionError("Invalid update channel")
    if not isinstance(info.get("repository"), str):
        raise VersionError("Invalid release identity")
    return {**info, "version": str(version), "file_version": (*version.core, sequence)}


def file_version_tuple(info):
    """Four-part Windows resource version derived from the single JSON source."""
    parsed = SemVer(info["version"])
    return (*parsed.core, info["release_sequence"])
