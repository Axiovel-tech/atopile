# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""
Agent-facing footprint placement tools for existing `.kicad_pcb` files:
query footprints, move/remove them by reference, and check/fix left-right
mirror symmetry of placement against the board outline.

These tools are deliberately independent of the atopile build graph so they
also work on hand-drawn KiCad boards (`ato layout ... --pcb board.kicad_pcb`).
"""

import fnmatch
import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from faebryk.libs.kicad.fileformats import kicad

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class PlacementError(Exception):
    pass


# -- queries ---------------------------------------------------------------------


@dataclass
class FootprintEntry:
    uuid: str
    reference: str
    value: str
    name: str  #: full library id, e.g. "axiovel_lib:TS_MINI_250"
    x: float
    y: float
    r: float
    layer: str
    outside_outline: bool | None  #: None if the board has no outline (bbox test)

    @property
    def base_name(self) -> str:
        return self.name.split(":")[-1]


def _prop(fp: "kicad.pcb.Footprint", name: str) -> str:
    return next((p.value for p in fp.propertys if p.name == name), "")


def _edge_items(pcb: "kicad.pcb.KicadPcb"):
    lines = [l for l in pcb.gr_lines if l.layer == "Edge.Cuts"]
    arcs = [a for a in pcb.gr_arcs if a.layer == "Edge.Cuts"]
    return lines, arcs


def list_footprints(
    pcb: "kicad.pcb.KicadPcb",
    like: str | None = None,
) -> list[FootprintEntry]:
    """
    All footprints with their placement. `like` filters with a glob matched
    against the reference, the value and the footprint name.
    """
    from faebryk.exporters.pcb.routing_tools import board_bbox

    bbox = board_bbox(pcb)
    out: list[FootprintEntry] = []
    for fp in pcb.footprints:
        ref = _prop(fp, "Reference")
        value = _prop(fp, "Value")
        if like is not None and not any(
            fnmatch.fnmatch(s, like) for s in (ref, value, fp.name, fp.name.split(":")[-1])
        ):
            continue
        outside = None
        if bbox is not None:
            outside = not (
                bbox[0] <= fp.at.x <= bbox[2] and bbox[1] <= fp.at.y <= bbox[3]
            )
        out.append(
            FootprintEntry(
                uuid=fp.uuid or "",
                reference=ref,
                value=value,
                name=fp.name,
                x=fp.at.x,
                y=fp.at.y,
                r=fp.at.r or 0,
                layer=fp.layer,
                outside_outline=outside,
            )
        )
    out.sort(key=lambda e: (e.reference, e.uuid))
    return out


def find_footprints(
    pcb: "kicad.pcb.KicadPcb",
    ref: str | None = None,
    uuid_prefix: str | None = None,
) -> list["kicad.pcb.Footprint"]:
    """Select footprints by exact reference and/or uuid prefix."""
    found = []
    for fp in pcb.footprints:
        if ref is not None and _prop(fp, "Reference") != ref:
            continue
        if uuid_prefix is not None and not (fp.uuid or "").startswith(uuid_prefix):
            continue
        found.append(fp)
    return found


def find_one_footprint(
    pcb: "kicad.pcb.KicadPcb",
    ref: str | None = None,
    uuid_prefix: str | None = None,
) -> "kicad.pcb.Footprint":
    """Like find_footprints, but demand exactly one match."""
    if ref is None and uuid_prefix is None:
        raise PlacementError("Select a footprint by reference or uuid")
    found = find_footprints(pcb, ref=ref, uuid_prefix=uuid_prefix)
    sel = " ".join(s for s in (ref, uuid_prefix) if s)
    if not found:
        raise PlacementError(f"No footprint matches `{sel}`")
    if len(found) > 1:
        uuids = ", ".join((f.uuid or "?")[:8] for f in found)
        raise PlacementError(
            f"`{sel}` is ambiguous ({len(found)} footprints: uuids {uuids});"
            " disambiguate with a uuid prefix"
        )
    return found[0]


# -- move / remove ---------------------------------------------------------------


def move_footprint(
    fp: "kicad.pcb.Footprint",
    x: float | None = None,
    y: float | None = None,
    dx: float = 0,
    dy: float = 0,
    r: float | None = None,
    layer: str | None = None,
) -> str:
    """
    Move/rotate a footprint (absolute x/y, relative dx/dy) and optionally flip
    it to another copper layer. Returns a human-readable change description.
    """
    from faebryk.exporters.pcb.kicad.transformer import PCB_Transformer

    old = (fp.at.x, fp.at.y, fp.at.r or 0, fp.layer)
    new_x = (x if x is not None else fp.at.x) + dx
    new_y = (y if y is not None else fp.at.y) + dy
    new_r = r if r is not None else (fp.at.r or 0)
    new_layer = layer if layer is not None else fp.layer

    PCB_Transformer.move_fp(
        fp, kicad.pcb.Xyr(x=new_x, y=new_y, r=new_r % 360), new_layer
    )
    return (
        f"({old[0]:g},{old[1]:g} r{old[2]:g} {old[3]})"
        f" -> ({new_x:g},{new_y:g} r{new_r % 360:g} {new_layer})"
    )


def remove_footprint(pcb: "kicad.pcb.KicadPcb", fp: "kicad.pcb.Footprint") -> None:
    uuid = fp.uuid
    kicad.filter(pcb, "footprints", pcb.footprints, lambda f: f.uuid != uuid)


# -- symmetry --------------------------------------------------------------------


@dataclass
class SymmetryPair:
    left: FootprintEntry
    right: FootprintEntry
    #: how far `right` is from the exact mirror position of `left`
    dx: float
    dy: float
    #: whether the rotations follow a recognised mirror relation
    rot_relation: str  #: "-r", "180-r", "none"

    @property
    def deviation(self) -> float:
        return math.hypot(self.dx, self.dy)


@dataclass
class SymmetryReport:
    axis: float
    axis_source: str  #: "explicit" | "outline-bbox"
    #: max distance any Edge.Cuts endpoint is from its mirrored counterpart
    edge_deviation: float | None
    edge_unmatched: int
    pairs: list[SymmetryPair] = field(default_factory=list)
    #: footprints close to the axis, with their |x - axis| offset
    centered: list[tuple[FootprintEntry, float]] = field(default_factory=list)
    #: footprints in scope with no mirror partner
    unpaired: list[FootprintEntry] = field(default_factory=list)


def _mirror_deviation_of_edges(
    pcb: "kicad.pcb.KicadPcb", axis: float, tol: float
) -> tuple[float | None, int]:
    """Match every Edge.Cuts primitive against its mirror image."""
    lines, arcs = _edge_items(pcb)

    def m(p) -> tuple[float, float]:
        return (2 * axis - p.x, p.y)

    def pdist(a: tuple[float, float], b: tuple[float, float]) -> float:
        return math.hypot(a[0] - b[0], a[1] - b[1])

    worst = 0.0
    unmatched = 0
    any_item = False

    line_pts = [((l.start.x, l.start.y), (l.end.x, l.end.y)) for l in lines]
    for l in lines:
        any_item = True
        ms, me = m(l.start), m(l.end)
        best = None
        for s, e in line_pts:
            d = min(
                max(pdist(ms, s), pdist(me, e)),
                max(pdist(ms, e), pdist(me, s)),
            )
            if best is None or d < best:
                best = d
        if best is None or best > tol:
            unmatched += 1
        else:
            worst = max(worst, best)

    arc_pts = [
        ((a.start.x, a.start.y), (a.mid.x, a.mid.y), (a.end.x, a.end.y)) for a in arcs
    ]
    for a in arcs:
        any_item = True
        ms, mm, me = m(a.start), m(a.mid), m(a.end)
        best = None
        for s, mid, e in arc_pts:
            d = min(
                max(pdist(ms, s), pdist(mm, mid), pdist(me, e)),
                max(pdist(ms, e), pdist(mm, mid), pdist(me, s)),
            )
            if best is None or d < best:
                best = d
        if best is None or best > tol:
            unmatched += 1
        else:
            worst = max(worst, best)

    return (worst if any_item else None), unmatched


def _rot_relation(r_left: float, r_right: float) -> str:
    def eq(a: float, b: float) -> bool:
        return abs((a - b + 180) % 360 - 180) < 0.01

    if eq(r_right, -r_left):
        return "-r"
    if eq(r_right, 180 - r_left):
        return "180-r"
    return "none"


def symmetry_report(
    pcb: "kicad.pcb.KicadPcb",
    axis: float | None = None,
    include: str | None = None,
    pair_tol: float = 2.0,
    edge_tol: float = 0.5,
) -> SymmetryReport:
    """
    Pair footprints that mirror each other about a vertical axis and measure
    placement deviations.

    Footprints are grouped by (footprint, layer) and greedily paired by
    distance from the exact mirror position; only deviations below `pair_tol`
    (mm) count as a pair. `include` is a comma-separated list of globs matched
    against reference / value / footprint name.
    """
    if axis is None:
        from faebryk.exporters.pcb.routing_tools import board_bbox

        bbox = board_bbox(pcb)
        if bbox is None:
            raise PlacementError(
                "Board has no Edge.Cuts outline; pass an explicit axis"
            )
        axis = (bbox[0] + bbox[2]) / 2
        axis_source = "outline-bbox"
    else:
        axis_source = "explicit"

    edge_dev, edge_unmatched = _mirror_deviation_of_edges(pcb, axis, edge_tol)

    entries = []
    for pattern in include.split(",") if include else [None]:
        entries += list_footprints(pcb, like=pattern.strip() if pattern else None)
    # de-dup (multiple patterns can match the same footprint)
    entries = list({e.uuid: e for e in entries}.values())

    report = SymmetryReport(
        axis=axis,
        axis_source=axis_source,
        edge_deviation=edge_dev,
        edge_unmatched=edge_unmatched,
    )

    groups: dict[tuple[str, str], list[FootprintEntry]] = {}
    for e in entries:
        groups.setdefault((e.name, e.layer), []).append(e)

    for group in groups.values():
        candidates: list[tuple[float, FootprintEntry, FootprintEntry]] = []
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                # deviation of b from the mirror position of a
                d = math.hypot(b.x - (2 * axis - a.x), b.y - a.y)
                if d <= pair_tol:
                    candidates.append((d, a, b))
        candidates.sort(key=lambda c: (c[0], c[1].uuid, c[2].uuid))

        used: set[str] = set()
        for d, a, b in candidates:
            if a.uuid in used or b.uuid in used:
                continue
            used.add(a.uuid)
            used.add(b.uuid)
            left, right = (a, b) if a.x <= b.x else (b, a)
            report.pairs.append(
                SymmetryPair(
                    left=left,
                    right=right,
                    dx=right.x - (2 * axis - left.x),
                    dy=right.y - left.y,
                    rot_relation=_rot_relation(left.r, right.r),
                )
            )

        for e in group:
            if e.uuid in used:
                continue
            if abs(e.x - axis) <= pair_tol:
                report.centered.append((e, e.x - axis))
            else:
                report.unpaired.append(e)

    report.pairs.sort(key=lambda p: -p.deviation)
    report.centered.sort(key=lambda c: -abs(c[1]))
    report.unpaired.sort(key=lambda e: (e.reference, e.uuid))
    return report


def apply_symmetry_fix(
    pcb: "kicad.pcb.KicadPcb",
    report: SymmetryReport,
    keep: str = "left",
) -> list[str]:
    """
    Snap every pair in `report` to perfect mirror symmetry, keeping the
    `keep` ("left"/"right") side fixed, and snap centered footprints onto the
    axis. Rotations are not changed. Returns change descriptions.
    """
    if keep not in ("left", "right"):
        raise PlacementError("keep must be 'left' or 'right'")

    changes: list[str] = []
    for pair in report.pairs:
        ref_e, move_e = (
            (pair.left, pair.right) if keep == "left" else (pair.right, pair.left)
        )
        target_x = 2 * report.axis - ref_e.x
        target_y = ref_e.y
        if math.isclose(move_e.x, target_x, abs_tol=1e-6) and math.isclose(
            move_e.y, target_y, abs_tol=1e-6
        ):
            continue
        fp = find_one_footprint(pcb, uuid_prefix=move_e.uuid)
        desc = move_footprint(fp, x=target_x, y=target_y)
        changes.append(f"{move_e.reference or move_e.uuid[:8]}: {desc}")

    for entry, offset in report.centered:
        if math.isclose(offset, 0, abs_tol=1e-6):
            continue
        fp = find_one_footprint(pcb, uuid_prefix=entry.uuid)
        desc = move_footprint(fp, x=report.axis)
        changes.append(f"{entry.reference or entry.uuid[:8]}: {desc}")

    return changes
