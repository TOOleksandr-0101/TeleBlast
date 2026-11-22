"""Read unified diffs and check their old coordinates against the pinned sources."""

from dataclasses import dataclass, field
import re


HEADER = re.compile(r"^@@ -(\d{1,10})(?:,(\d{1,10}))? \+(\d{1,10})(?:,(\d{1,10}))? @@")


class UnsupportedPatch(ValueError):
    """The patch cannot safely be narrowed using the pinned-source index."""


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    body: list[str] = field(default_factory=list)

    def edited_lines(self, side):
        """Yield exact edited line ranges for ``old`` or ``new`` coordinates.

        Unlike :meth:`changes`, pure insertions have no old edited range and
        pure deletions have no new edited range.  Precision analysis uses both
        sides to compare the syntax that was actually added or removed, while
        line coverage continues to use ``changes()`` and its safe neighbours.
        """
        if side not in ("old", "new"):
            raise ValueError("side must be 'old' or 'new'")
        line = self.old_start if side == "old" else self.new_start
        marker = "-" if side == "old" else "+"
        other = "+" if side == "old" else "-"
        begin = previous = None
        for entry in [*self.body, " "]:
            prefix = entry[0]
            if prefix == marker:
                if begin is None:
                    begin = line
                previous = line
            elif begin is not None:
                yield begin, previous
                begin = previous = None
            if prefix != other:
                line += 1

    def changes(self):
        """Yield old-source intervals, including both neighbors for pure insertions."""
        yield from self._changes("old")

    def new_changes(self):
        """Yield new-source intervals, including both neighbors for pure deletions."""
        yield from self._changes("new")

    def _changes(self, side):
        old = side == "old"
        count = self.old_count if old else self.new_count
        line = (self.old_start if old else self.new_start) if count else (
            self.old_start if old else self.new_start
        ) + 1
        advancing = "-" if old else "+"
        begin = None
        for entry in [*self.body, " "]:
            if entry[0] == " ":
                if begin is not None:
                    if begin == line:
                        yield max(1, line - 1), line
                    else:
                        yield begin, line - 1
                begin = None
                line += 1
            else:
                if begin is None:
                    begin = line
                if entry[0] == advancing:
                    line += 1


@dataclass
class Change:
    path: str
    hunks: list[Hunk] = field(default_factory=list)

    def updated_python(self, source):
        lines = source.split("\n")
        previous = 0
        result = []
        for hunk in self.hunks:
            start = hunk.old_start - (1 if hunk.old_count else 0)
            if start < previous or start > len(lines):
                raise UnsupportedPatch("invalid old coordinates")
            result.extend(lines[previous:start])
            if len(result) != hunk.new_start - (1 if hunk.new_count else 0):
                raise UnsupportedPatch("inconsistent new coordinates")
            before = [x[1:] for x in hunk.body if x[0] != "+"]
            if lines[start:start + len(before)] != before:
                raise UnsupportedPatch("source differs from pinned revision")
            result.extend(x[1:] for x in hunk.body if x[0] != "-")
            previous = start + len(before)
        result.extend(lines[previous:])
        return "\n".join(result)


def _path(line, git_prefix):
    name = line[4:].split("\t", 1)[0].removeprefix(git_prefix)
    if not name or name.startswith(('"', '/')) or ".." in name.split("/"):
        raise UnsupportedPatch("new, deleted, or ambiguous file")
    return name


def parse_patch(text):
    changes = []
    current = None
    lines = text.split("\n")
    pos = 0
    awaiting_headers = False
    while pos < len(lines):
        line = lines[pos]
        pos += 1
        if line.startswith("diff --git "):
            if awaiting_headers:
                raise UnsupportedPatch("missing file headers")
            awaiting_headers = True
            current = None
        elif line.startswith("--- "):
            if pos == len(lines) or not lines[pos].startswith("+++ "):
                raise UnsupportedPatch("unpaired file headers")
            old = _path(line, "a/")
            new = _path(lines[pos], "b/")
            pos += 1
            if old != new:
                raise UnsupportedPatch("renamed file")
            current = Change(old)
            changes.append(current)
            awaiting_headers = False
        elif line.startswith("@@"):
            match = HEADER.match(line)
            if not match or current is None:
                raise UnsupportedPatch("invalid hunk header")
            a, b, c, d = match.groups()
            hunk = Hunk(int(a), int(b or 1), int(c), int(d or 1))
            old_left, new_left = hunk.old_count, hunk.new_count
            while old_left or new_left:
                if pos == len(lines) or not lines[pos]:
                    raise UnsupportedPatch("truncated hunk")
                entry = lines[pos]
                pos += 1
                if entry == r"\ No newline at end of file":
                    continue
                prefix = entry[0]
                if prefix not in " +-":
                    raise UnsupportedPatch("unsupported hunk entry")
                old_left -= int(prefix != "+")
                new_left -= int(prefix != "-")
                if old_left < 0 or new_left < 0:
                    raise UnsupportedPatch("incorrect hunk length")
                hunk.body.append(entry)
            if not any(x[0] in "+-" for x in hunk.body):
                raise UnsupportedPatch("no change in hunk")
            current.hunks.append(hunk)
        elif line == "" or line == r"\ No newline at end of file" or line.startswith("index "):
            continue
        else:
            # Includes git binary, copy, rename-only and mode changes.
            raise UnsupportedPatch("unsupported diff metadata")
    if awaiting_headers or not changes or any(not change.hunks for change in changes):
        raise UnsupportedPatch("no complete changed file")
    return changes
