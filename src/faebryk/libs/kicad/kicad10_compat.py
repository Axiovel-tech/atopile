# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""
Load-time compatibility shim for KiCad >=10 board files.

KiCad 10 (file version >= 20250000) changed how nets are stored in
`.kicad_pcb` files:
 - the global net declaration table `(net <number> "<name>")` was removed
 - all net references (pads, vias, segments, arcs, zones) became name-only:
   `(net "<name>")` instead of `(net <number> ["<name>"])`
 - zones no longer carry a separate `(net_name "...")` token
 - netless tracks/zones omit the `(net ...)` token entirely

The typed sexp models (and all code built on them) are net-number-based, so
this shim rewrites a KiCad 10 board file into the equivalent KiCad 9 style
representation at load time:
 - synthesizes a net table from all referenced net names
 - rewrites net references with their synthesized numbers
 - re-adds `(net_name ...)` to zones and `(net 0)` to netless elements
 - downgrades the version stamp so the document is self-consistent

atopile keeps *writing* KiCad 9 style files, which KiCad 10 opens and
upgrades transparently.
"""

import logging

from faebryk.libs.kicad.sexp_tools import (
    SexpAtom,
    SexpNode,
    dump_sexp,
    parse_sexp,
    sexp_tag,
)

logger = logging.getLogger(__name__)

#: version stamp emitted for the downgraded document (matches typed model)
TARGET_VERSION = "20241229"

#: file versions >= this are considered "KiCad 10 style"
KICAD10_VERSION_THRESHOLD = 20250000

#: elements whose `(net ...)` child carries `<number>` only (KiCad 9 style)
_NUMBER_ONLY_NET_PARENTS = ("via", "segment", "arc")


def is_kicad10_pcb(text: str) -> bool:
    """Cheap check whether a .kicad_pcb document uses KiCad 10 conventions."""
    import re

    m = re.search(r"\(\s*version\s+(\d+)\s*\)", text[:512])
    if m:
        return int(m.group(1)) >= KICAD10_VERSION_THRESHOLD
    return False


def _find_net_child(node: list[SexpNode]) -> tuple[int, list[SexpNode]] | None:
    for i, child in enumerate(node):
        if sexp_tag(child) == "net":
            return i, child  # type: ignore[return-value]
    return None


class _NetTable:
    def __init__(self) -> None:
        self._by_name: dict[str, int] = {"": 0}

    def number(self, name: str) -> int:
        if name not in self._by_name:
            self._by_name[name] = len(self._by_name)
        return self._by_name[name]

    def declarations(self) -> list[list[SexpNode]]:
        return [
            [
                SexpAtom.symbol("net"),
                SexpAtom.symbol(str(number)),
                SexpAtom.string(name),
            ]
            for name, number in self._by_name.items()
        ]


def _net_ref_name(net_node: list[SexpNode]) -> str | None:
    """Name of a name-only net reference `(net "name")`, else None."""
    args = [c for c in net_node[1:] if isinstance(c, SexpAtom)]
    if len(args) == 1 and args[0].quoted:
        return args[0].value
    return None


def _upgrade_element(
    element: list[SexpNode], nets: _NetTable, *, parent_tag: str
) -> None:
    found = _find_net_child(element)

    if found is None:
        # netless elements must carry an explicit (net 0) in KiCad 9 style
        if parent_tag in _NUMBER_ONLY_NET_PARENTS:
            element.append([SexpAtom.symbol("net"), SexpAtom.symbol("0")])
        elif parent_tag == "zone":
            element.insert(1, [SexpAtom.symbol("net"), SexpAtom.symbol("0")])
            element.insert(2, [SexpAtom.symbol("net_name"), SexpAtom.string("")])
        return

    idx, net_node = found
    name = _net_ref_name(net_node)
    if name is None:
        # already numbered (KiCad 9 style); leave as-is
        return

    number = nets.number(name)

    if parent_tag == "pad":
        element[idx] = [
            SexpAtom.symbol("net"),
            SexpAtom.symbol(str(number)),
            SexpAtom.string(name),
        ]
    elif parent_tag == "zone":
        element[idx] = [SexpAtom.symbol("net"), SexpAtom.symbol(str(number))]
        element.insert(
            idx + 1, [SexpAtom.symbol("net_name"), SexpAtom.string(name)]
        )
    else:
        element[idx] = [SexpAtom.symbol("net"), SexpAtom.symbol(str(number))]


def upgrade_kicad10_pcb_text(text: str) -> str:
    """
    Rewrite a KiCad 10 style .kicad_pcb document into KiCad 9 style.

    Returns the text unchanged if it is not a KiCad 10 style document.
    """
    if not is_kicad10_pcb(text):
        return text

    root = parse_sexp(text)
    if sexp_tag(root) != "kicad_pcb":
        return text
    assert isinstance(root, list)

    nets = _NetTable()

    last_top_level_net_decl_idx: int | None = None

    for i, node in enumerate(root):
        tag = sexp_tag(node)
        if tag is None:
            continue
        assert isinstance(node, list)

        if tag == "version":
            node[1:] = [SexpAtom.symbol(TARGET_VERSION)]
        elif tag == "net":
            # existing declaration (shouldn't happen in v10, but be safe)
            last_top_level_net_decl_idx = i
        elif tag in _NUMBER_ONLY_NET_PARENTS or tag == "zone":
            _upgrade_element(node, nets, parent_tag=tag)
        elif tag == "footprint":
            for child in node:
                if sexp_tag(child) == "pad":
                    assert isinstance(child, list)
                    _upgrade_element(child, nets, parent_tag="pad")

    # inject the synthesized net table before the first footprint
    # (KiCad expects declarations before usage)
    decls = nets.declarations()
    if last_top_level_net_decl_idx is not None:
        insert_at = last_top_level_net_decl_idx + 1
        # drop net 0 if already declared
        decls = [d for d in decls if d[1].raw != "0"]
    else:
        insert_at = next(
            (
                i
                for i, node in enumerate(root)
                if sexp_tag(node) in ("footprint", "segment", "via", "arc", "zone")
            ),
            len(root),
        )
    root[insert_at:insert_at] = decls

    logger.debug(
        f"Upgraded KiCad 10 board file to KiCad 9 style ({len(decls)} nets)"
    )

    return dump_sexp(root) + "\n"
