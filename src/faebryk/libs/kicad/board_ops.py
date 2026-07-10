# This file is part of the faebryk project
# SPDX-License-Identifier: MIT
"""Graph-independent operations on native KiCad boards.

These helpers operate directly on `.kicad_pcb` files through
`faebryk.libs.kicad.fileformats` without requiring an ato project, the
faebryk graph, or a KiCad installation. They exist so the atopile
toolchain is useful for KiCad-native PCB work: boards whose source of
truth is a hand- or script-authored KiCad project.

Provided operations:
- summarize:            board statistics (outline size, layers, counts)
- set_rectangular_outline: draw a (rounded) rectangular Edge.Cuts outline
- copy_setup_text:      transplant stackup/setup + layer table from a donor
                        board (KiCad's "Import Settings" for headless flows)
KiCad 10 files load transparently via the kicad10_compat shim in
`faebryk.libs.kicad.fileformats`.
- dump_placement / apply_placement: reviewable, diff-able placement
                        round-trip keyed by reference designator
- courtyard_bboxes / check_courtyard_overlaps: fast placement gate
"""

import math
import uuid as _uuid
from dataclasses import dataclass, field

from faebryk.libs.kicad.fileformats import kicad

_EDGE_CUTS = "Edge.Cuts"
_EDGE_STROKE_WIDTH = 0.05
_HALF_SQRT2 = math.sqrt(2.0) / 2.0
_COURTYARD_LAYERS = ("F.CrtYd", "B.CrtYd")
# fallback margin around the pad bbox when a footprint has no courtyard
_NO_COURTYARD_MARGIN_MM = 0.25

type PcbFile = kicad.pcb.PcbFile
type BBox = tuple[float, float, float, float]  # x0, y0, x1, y1


def _gen_uuid() -> str:
    return str(_uuid.uuid4())


def load_board_text(text: str) -> PcbFile:
    """Load a `.kicad_pcb` from text.

    KiCad 10 documents are handled transparently by `kicad.loads` (see
    `faebryk.libs.kicad.kicad10_compat`).
    """
    return kicad.loads(kicad.pcb.PcbFile, text)


# --------------------------------------------------------------------- stats
@dataclass
class BoardSummary:
    outline_bbox: BBox | None
    size_mm: tuple[float, float] | None
    copper_layers: list[str]
    layer_count: int
    footprints: int
    footprints_front: int
    footprints_back: int
    nets: int
    pads_total: int
    pads_unconnected: int
    tracks: int
    vias: int
    zones: int

    def as_dict(self) -> dict:
        return {
            "outline_bbox": self.outline_bbox,
            "size_mm": self.size_mm,
            "copper_layers": self.copper_layers,
            "layer_count": self.layer_count,
            "footprints": self.footprints,
            "footprints_front": self.footprints_front,
            "footprints_back": self.footprints_back,
            "nets": self.nets,
            "pads_total": self.pads_total,
            "pads_unconnected": self.pads_unconnected,
            "tracks": self.tracks,
            "vias": self.vias,
            "zones": self.zones,
        }


def _outline_bbox(pcb: PcbFile) -> BBox | None:
    xs: list[float] = []
    ys: list[float] = []
    board = pcb.kicad_pcb
    for line in board.gr_lines:
        if line.layer != _EDGE_CUTS:
            continue
        xs += [line.start.x, line.end.x]
        ys += [line.start.y, line.end.y]
    for arc in board.gr_arcs:
        if arc.layer != _EDGE_CUTS:
            continue
        xs += [arc.start.x, arc.mid.x, arc.end.x]
        ys += [arc.start.y, arc.mid.y, arc.end.y]
    for rect in board.gr_rects:
        if rect.layer != _EDGE_CUTS:
            continue
        xs += [rect.start.x, rect.end.x]
        ys += [rect.start.y, rect.end.y]
    for circle in board.gr_circles:
        if circle.layer != _EDGE_CUTS:
            continue
        r = math.dist((circle.center.x, circle.center.y), (circle.end.x, circle.end.y))
        xs += [circle.center.x - r, circle.center.x + r]
        ys += [circle.center.y - r, circle.center.y + r]
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def summarize(pcb: PcbFile) -> BoardSummary:
    board = pcb.kicad_pcb
    bbox = _outline_bbox(pcb)
    size = (round(bbox[2] - bbox[0], 4), round(bbox[3] - bbox[1], 4)) if bbox else None
    copper = [layer.name for layer in board.layers if layer.name.endswith(".Cu")]
    pads_total = 0
    pads_unconnected = 0
    front = 0
    back = 0
    for fp in board.footprints:
        if fp.layer == "F.Cu":
            front += 1
        elif fp.layer == "B.Cu":
            back += 1
        for pad in fp.pads:
            pads_total += 1
            if pad.net is None:
                pads_unconnected += 1
    return BoardSummary(
        outline_bbox=bbox,
        size_mm=size,
        copper_layers=copper,
        layer_count=len(board.layers),
        footprints=len(board.footprints),
        footprints_front=front,
        footprints_back=back,
        nets=len(board.nets),
        pads_total=pads_total,
        pads_unconnected=pads_unconnected,
        tracks=len(board.segments) + len(board.arcs),
        vias=len(board.vias),
        zones=len(board.zones),
    )


# ------------------------------------------------------------------- outline
def _edge_kwargs() -> dict:
    return {
        "stroke": kicad.pcb.Stroke(
            width=_EDGE_STROKE_WIDTH,
            type=kicad.pcb.E_stroke_type.SOLID,
        ),
        "layer": _EDGE_CUTS,
        "uuid": _gen_uuid(),
        "solder_mask_margin": None,
        "fill": None,
        "locked": None,
        "layers": [],
    }


def _edge_line(start: tuple[float, float], end: tuple[float, float]):
    return kicad.pcb.Line(
        start=kicad.pcb.Xy(x=start[0], y=start[1]),
        end=kicad.pcb.Xy(x=end[0], y=end[1]),
        **_edge_kwargs(),
    )


def _edge_arc(
    start: tuple[float, float], mid: tuple[float, float], end: tuple[float, float]
):
    return kicad.pcb.Arc(
        start=kicad.pcb.Xy(x=start[0], y=start[1]),
        mid=kicad.pcb.Xy(x=mid[0], y=mid[1]),
        end=kicad.pcb.Xy(x=end[0], y=end[1]),
        **_edge_kwargs(),
    )


def clear_outline(pcb: PcbFile) -> int:
    """Remove all board-level Edge.Cuts primitives. Returns removed count."""
    board = pcb.kicad_pcb
    removed = 0
    for attr in ("gr_lines", "gr_arcs", "gr_rects", "gr_circles", "gr_curves"):
        container = getattr(board, attr)
        before = len(container)
        kicad.filter(board, attr, container, lambda g: g.layer != _EDGE_CUTS)
        removed += before - len(getattr(board, attr))
    return removed


def set_rectangular_outline(
    pcb: PcbFile,
    *,
    width_mm: float,
    height_mm: float,
    corner_radius_mm: float = 0.0,
    origin: tuple[float, float] = (0.0, 0.0),
    replace: bool = True,
) -> None:
    """Draw a (rounded-)rectangular outline with the top-left corner at
    `origin`. Replaces any existing board-level Edge.Cuts geometry unless
    `replace=False`."""
    if width_mm <= 0 or height_mm <= 0:
        raise ValueError("width and height must be > 0")
    r = corner_radius_mm
    if r < 0:
        raise ValueError("corner_radius must be >= 0")
    if r > min(width_mm, height_mm) / 2:
        raise ValueError(
            f"corner_radius ({r} mm) exceeds half of the smallest board "
            f"dimension ({min(width_mm, height_mm) / 2} mm)"
        )
    if replace:
        clear_outline(pcb)

    ox, oy = origin
    w, h = width_mm, height_mm
    board = pcb.kicad_pcb
    offset = r - r * _HALF_SQRT2

    kicad.insert(
        board,
        "gr_lines",
        board.gr_lines,
        _edge_line((ox + r, oy), (ox + w - r, oy)),
        _edge_line((ox + w, oy + r), (ox + w, oy + h - r)),
        _edge_line((ox + w - r, oy + h), (ox + r, oy + h)),
        _edge_line((ox, oy + h - r), (ox, oy + r)),
    )
    if r > 0:
        kicad.insert(
            board,
            "gr_arcs",
            board.gr_arcs,
            _edge_arc(
                (ox + w - r, oy),
                (ox + w - offset, oy + offset),
                (ox + w, oy + r),
            ),
            _edge_arc(
                (ox + w, oy + h - r),
                (ox + w - offset, oy + h - offset),
                (ox + w - r, oy + h),
            ),
            _edge_arc(
                (ox + r, oy + h),
                (ox + offset, oy + h - offset),
                (ox, oy + h - r),
            ),
            _edge_arc(
                (ox, oy + r),
                (ox + offset, oy + offset),
                (ox + r, oy),
            ),
        )


# ----------------------------------------------------------------- stackup
def _extract_block(text: str, token: str) -> str:
    """Extract the first top-level-ish `(token ...)` block, balanced."""
    idx = text.find(f"({token}")
    if idx < 0:
        raise ValueError(f"no ({token} ...) block found")
    depth = 0
    for j in range(idx, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return text[idx : j + 1]
    raise ValueError(f"unbalanced ({token} ...) block")


def copy_setup_text(
    dst_text: str, src_text: str, rename_layers: dict[str, str] | None = None
) -> str:
    """Transplant the `(layers ...)` and `(setup ...)` blocks from a donor
    board into `dst_text` (headless equivalent of KiCad's Board Setup ->
    Import Settings). `rename_layers` maps display names, e.g.
    {"L3_SIG": "L3_PWR"}. The result is validated by re-parsing.
    """
    src_layers = _extract_block(src_text, "layers")
    src_setup = _extract_block(src_text, "setup")
    for old, new in (rename_layers or {}).items():
        if f'"{old}"' not in src_layers:
            raise ValueError(f"layer name {old!r} not found in donor layer table")
        src_layers = src_layers.replace(f'"{old}"', f'"{new}"')

    dst_layers = _extract_block(dst_text, "layers")
    out = dst_text.replace(dst_layers, src_layers, 1)
    try:
        dst_setup = _extract_block(out, "setup")
        out = out.replace(dst_setup, src_setup, 1)
    except ValueError:
        # destination has no setup block yet: put it right after the layers
        new_layers = _extract_block(out, "layers")
        out = out.replace(new_layers, new_layers + "\n\t" + src_setup, 1)

    kicad.loads(kicad.pcb.PcbFile, out)  # validate
    return out


# --------------------------------------------------------------- placement
@dataclass
class Placement:
    x: float
    y: float
    rotation: float = 0.0
    layer: str = "F.Cu"

    def as_dict(self) -> dict:
        return {
            "x": self.x,
            "y": self.y,
            "rotation": self.rotation,
            "layer": self.layer,
        }


def _fp_ref(fp) -> str | None:
    for prop in fp.propertys:
        if prop.name == "Reference":
            return prop.value
    return None


def dump_placement(pcb: PcbFile) -> dict[str, Placement]:
    """Placement snapshot keyed by reference designator."""
    out: dict[str, Placement] = {}
    for fp in pcb.kicad_pcb.footprints:
        ref = _fp_ref(fp)
        if ref is None:
            continue
        out[ref] = Placement(
            x=fp.at.x,
            y=fp.at.y,
            rotation=fp.at.r or 0.0,
            layer=fp.layer,
        )
    return out


def apply_placement(
    pcb: PcbFile,
    placement: dict[str, Placement],
    *,
    strict: bool = True,
) -> list[str]:
    """Move/rotate footprints to `placement`. Returns refs that were applied.

    Pad and text angles stored in the file compose the footprint angle, so a
    rotation delta is propagated to them (positions of children are relative
    and follow the footprint automatically).

    Side changes (layer flips) are not supported; flipping remaps pad layers
    and mirrors geometry, which is KiCad-version-specific. Do flips in KiCad.
    """
    by_ref = {}
    for fp in pcb.kicad_pcb.footprints:
        ref = _fp_ref(fp)
        if ref is not None:
            by_ref[ref] = fp

    applied = []
    for ref, place in placement.items():
        fp = by_ref.get(ref)
        if fp is None:
            if strict:
                raise KeyError(f"footprint {ref!r} not on board")
            continue
        if place.layer != fp.layer:
            raise NotImplementedError(
                f"{ref}: changing side ({fp.layer} -> {place.layer}) is not "
                "supported; flip footprints in KiCad"
            )
        delta = (place.rotation - (fp.at.r or 0.0)) % 360.0
        fp.at.x = place.x
        fp.at.y = place.y
        fp.at.r = place.rotation % 360.0
        if delta:
            for pad in fp.pads:
                pad.at.r = ((pad.at.r or 0.0) + delta) % 360.0
            for text in fp.fp_texts:
                text.at.r = ((text.at.r or 0.0) + delta) % 360.0
            for prop in fp.propertys:
                prop.at.r = ((prop.at.r or 0.0) + delta) % 360.0
        applied.append(ref)
    return applied


# -------------------------------------------------------------- courtyards
@dataclass
class CourtyardOverlap:
    ref_a: str
    ref_b: str
    area_mm2: float
    bbox: BBox = field(default=(0.0, 0.0, 0.0, 0.0))


def _geo_points(geo) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    if hasattr(geo, "start") and hasattr(geo, "end"):
        pts += [(geo.start.x, geo.start.y), (geo.end.x, geo.end.y)]
    if hasattr(geo, "mid"):
        pts.append((geo.mid.x, geo.mid.y))
    if hasattr(geo, "center"):
        r = math.dist((geo.center.x, geo.center.y), (geo.end.x, geo.end.y))
        pts += [
            (geo.center.x - r, geo.center.y - r),
            (geo.center.x + r, geo.center.y + r),
        ]
    if hasattr(geo, "pts") and geo.pts is not None:
        xys = getattr(geo.pts, "xys", None) or []
        pts += [(p.x, p.y) for p in xys]
    return pts


def _fp_geos(fp):
    return (
        list(fp.fp_lines)
        + list(fp.fp_arcs)
        + list(fp.fp_circles)
        + list(fp.fp_rects)
        + list(fp.fp_poly)
    )


def courtyard_bboxes(pcb: PcbFile) -> dict[str, BBox]:
    """Absolute courtyard bbox per footprint (pad bbox + margin fallback)."""
    out: dict[str, BBox] = {}
    for fp in pcb.kicad_pcb.footprints:
        ref = _fp_ref(fp)
        if ref is None:
            continue
        pts: list[tuple[float, float]] = []
        for geo in _fp_geos(fp):
            if geo.layer in _COURTYARD_LAYERS:
                pts += _geo_points(geo)
        if not pts:
            for pad in fp.pads:
                hw, hh = pad.size.w / 2, (pad.size.h or pad.size.w) / 2
                m = _NO_COURTYARD_MARGIN_MM
                pts += [
                    (pad.at.x - hw - m, pad.at.y - hh - m),
                    (pad.at.x + hw + m, pad.at.y + hh + m),
                ]
        if not pts:
            continue
        rot = math.radians(-(fp.at.r or 0.0))
        cos_r, sin_r = math.cos(rot), math.sin(rot)
        abs_pts = [
            (
                fp.at.x + px * cos_r - py * sin_r,
                fp.at.y + px * sin_r + py * cos_r,
            )
            for px, py in pts
        ]
        xs = [p[0] for p in abs_pts]
        ys = [p[1] for p in abs_pts]
        out[ref] = (min(xs), min(ys), max(xs), max(ys))
    return out


def check_courtyard_overlaps(
    pcb: PcbFile, *, min_area_mm2: float = 0.0
) -> list[CourtyardOverlap]:
    """Report overlapping courtyard bounding boxes between footprints on the
    same side. Conservative (bbox-level) but dependency-free and fast."""
    boxes = courtyard_bboxes(pcb)
    sides = {
        _fp_ref(fp): fp.layer
        for fp in pcb.kicad_pcb.footprints
        if _fp_ref(fp) is not None
    }
    refs = sorted(boxes)
    overlaps: list[CourtyardOverlap] = []
    for i, a in enumerate(refs):
        ax0, ay0, ax1, ay1 = boxes[a]
        for b in refs[i + 1 :]:
            if sides.get(a) != sides.get(b):
                continue
            bx0, by0, bx1, by1 = boxes[b]
            ox0, oy0 = max(ax0, bx0), max(ay0, by0)
            ox1, oy1 = min(ax1, bx1), min(ay1, by1)
            if ox0 >= ox1 or oy0 >= oy1:
                continue
            area = (ox1 - ox0) * (oy1 - oy0)
            if area > min_area_mm2:
                overlaps.append(
                    CourtyardOverlap(
                        ref_a=a,
                        ref_b=b,
                        area_mm2=round(area, 4),
                        bbox=(ox0, oy0, ox1, oy1),
                    )
                )
    return overlaps
