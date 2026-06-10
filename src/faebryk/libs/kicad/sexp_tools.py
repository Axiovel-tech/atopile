# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""
Minimal tolerant, lossless s-expression parser/serializer.

Unlike the strict Zig-backed typed models, this operates on raw nested lists
and preserves atom lexemes exactly as found in the source. It is used for
format-compatibility shims and for extracting data out of KiCad files the
typed models cannot (yet) represent.
"""

import re
from dataclasses import dataclass


class SexpParseError(Exception):
    pass


@dataclass
class SexpAtom:
    raw: str
    quoted: bool

    @property
    def value(self) -> str:
        if not self.quoted:
            return self.raw
        inner = self.raw[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")

    @staticmethod
    def symbol(value: str) -> "SexpAtom":
        return SexpAtom(raw=value, quoted=False)

    @staticmethod
    def string(value: str) -> "SexpAtom":
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return SexpAtom(raw=f'"{escaped}"', quoted=True)


type SexpNode = list["SexpNode"] | SexpAtom

_TOKEN_RE = re.compile(
    r"""
    (?P<open>\() |
    (?P<close>\)) |
    (?P<string>"(?:[^"\\]|\\.)*") |
    (?P<atom>[^\s()"]+) |
    (?P<ws>\s+)
    """,
    re.VERBOSE,
)


def parse_sexp(text: str) -> SexpNode:
    """Parse a single top-level s-expression into nested lists of SexpAtom."""
    stack: list[list[SexpNode]] = []
    root: SexpNode | None = None
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise SexpParseError(
                f"Failed to tokenize s-expression at offset {pos}: "
                f"{text[pos : pos + 40]!r}"
            )
        pos = m.end()
        if m.lastgroup == "ws":
            continue
        if m.lastgroup == "open":
            new: list[SexpNode] = []
            if stack:
                stack[-1].append(new)
            stack.append(new)
        elif m.lastgroup == "close":
            if not stack:
                raise SexpParseError("Unbalanced ')' in s-expression")
            closed = stack.pop()
            if not stack:
                if root is not None:
                    raise SexpParseError("Multiple top-level s-expressions found")
                root = closed
        else:
            atom = SexpAtom(raw=m.group(0), quoted=m.lastgroup == "string")
            if not stack:
                raise SexpParseError("Atom outside of any s-expression")
            stack[-1].append(atom)
    if stack or root is None:
        raise SexpParseError("Unbalanced '(' in s-expression")
    return root


def dump_sexp(node: SexpNode, indent: int = 0) -> str:
    """Serialize with KiCad-style (newline + tab) formatting."""
    if isinstance(node, SexpAtom):
        return node.raw

    if all(isinstance(c, SexpAtom) for c in node):
        return "(" + " ".join(c.raw for c in node if isinstance(c, SexpAtom)) + ")"

    parts: list[str] = []
    i = 0
    while i < len(node) and isinstance(node[i], SexpAtom):
        parts.append(node[i].raw)  # type: ignore[union-attr]
        i += 1
    head = "(" + " ".join(parts)
    body = ""
    for child in node[i:]:
        body += "\n" + "\t" * (indent + 1) + dump_sexp(child, indent + 1)
    return head + body + "\n" + "\t" * indent + ")"


def sexp_tag(node: SexpNode) -> str | None:
    """The leading symbol of a list node, if any."""
    if (
        isinstance(node, list)
        and node
        and isinstance(node[0], SexpAtom)
        and not node[0].quoted
    ):
        return node[0].raw
    return None


def sexp_children(node: SexpNode, tag: str) -> list[list[SexpNode]]:
    """All child list nodes with the given leading symbol."""
    if not isinstance(node, list):
        return []
    return [c for c in node if sexp_tag(c) == tag]  # type: ignore[misc]


def sexp_atom_arg(node: list[SexpNode], idx: int = 1) -> str | None:
    """The idx-th (1-based) atom argument of a list node, unquoted."""
    args = [c for c in node[1:] if isinstance(c, SexpAtom)]
    if len(args) < idx:
        return None
    return args[idx - 1].value
