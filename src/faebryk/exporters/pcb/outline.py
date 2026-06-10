# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""
Generate the board outline (Edge.Cuts) from build configuration.

The outline is described declaratively in `ato.yaml` per build target:

```yaml
builds:
  my_board:
    entry: main.ato:MyBoard
    board-outline:
      # either a rounded rectangle ...
      rounded-rect:
        x: 0          # top-left corner, mm
        y: 0
        width: 30
        height: 24
        radius: 1.0
      # ... or an arbitrary polygon with optional per-vertex fillets
      polygon:
        - at: [0, 0]
          fillet: 1.0
        - at: [30, 0]
        - at: [30, 24]
          fillet: 2.5
        - at: [0, 24]
```

Generated edges are uuid-marked so re-builds replace them cleanly.
Hand-drawn (unmarked) Edge.Cuts geometry is left untouched.
"""

import logging
import math
from dataclasses import dataclass

from faebryk.libs.kicad.fileformats import kicad

logger = logging.getLogger(__name__)

EDGE_LAYER = "Edge.Cuts"
EDGE_STROKE_WIDTH = 0.05


class BoardOutlineError(Exception):
    pass


@dataclass
class OutlineVertex:
    x: float
    y: float
    fillet: float = 0.0


def rounded_rect_vertices(
    x: float, y: float, width: float, height: float, radius: float = 0.0
) -> list[OutlineVertex]:
    return [
        OutlineVertex(x, y, radius),
        OutlineVertex(x + width, y, radius),
        OutlineVertex(x + width, y + height, radius),
        OutlineVertex(x, y + height, radius),
    ]


@dataclass
class _LineSeg:
    start: tuple[float, float]
    end: tuple[float, float]


@dataclass
class _ArcSeg:
    start: tuple[float, float]
    mid: tuple[float, float]
    end: tuple[float, float]


def _norm(vx: float, vy: float) -> tuple[float, float]:
    length = math.hypot(vx, vy)
    if length == 0:
        raise BoardOutlineError("Outline contains coincident consecutive vertices")
    return vx / length, vy / length


def outline_segments(
    vertices: list[OutlineVertex],
) -> list[_LineSeg | _ArcSeg]:
    """Convert a closed polygon with per-vertex fillets to line/arc segments."""
    n = len(vertices)
    if n < 3:
        raise BoardOutlineError("Outline polygon needs at least 3 vertices")

    # corner points of each vertex after filleting:
    # (entry tangent point, [arc], exit tangent point)
    corner_points: list[tuple[tuple[float, float], tuple[float, float]]] = []
    arcs: list[_ArcSeg | None] = []

    for i, v in enumerate(vertices):
        p_prev = vertices[(i - 1) % n]
        p_next = vertices[(i + 1) % n]

        if v.fillet <= 0:
            corner_points.append(((v.x, v.y), (v.x, v.y)))
            arcs.append(None)
            continue

        ax, ay = _norm(p_prev.x - v.x, p_prev.y - v.y)
        bx, by = _norm(p_next.x - v.x, p_next.y - v.y)

        cos_theta = max(-1.0, min(1.0, ax * bx + ay * by))
        theta = math.acos(cos_theta)
        if math.isclose(theta, math.pi, abs_tol=1e-6) or math.isclose(
            theta, 0, abs_tol=1e-6
        ):
            raise BoardOutlineError(
                f"Cannot fillet straight/degenerate corner at ({v.x}, {v.y})"
            )

        tangent_dist = v.fillet / math.tan(theta / 2)
        max_a = math.hypot(p_prev.x - v.x, p_prev.y - v.y)
        max_b = math.hypot(p_next.x - v.x, p_next.y - v.y)
        if tangent_dist > max_a or tangent_dist > max_b:
            raise BoardOutlineError(
                f"Fillet radius {v.fillet} too large for corner at ({v.x}, {v.y})"
            )

        t1 = (v.x + ax * tangent_dist, v.y + ay * tangent_dist)
        t2 = (v.x + bx * tangent_dist, v.y + by * tangent_dist)

        # arc center along the angle bisector
        cx, cy = _norm(ax + bx, ay + by)
        center_dist = v.fillet / math.sin(theta / 2)
        center = (v.x + cx * center_dist, v.y + cy * center_dist)

        # arc midpoint: on the arc, towards the vertex
        mid = (center[0] - cx * v.fillet, center[1] - cy * v.fillet)

        corner_points.append((t1, t2))
        arcs.append(_ArcSeg(start=t1, mid=mid, end=t2))

    segments: list[_LineSeg | _ArcSeg] = []
    for i in range(n):
        if (arc := arcs[i]) is not None:
            segments.append(arc)
        # connect this corner's exit point to the next corner's entry point
        start = corner_points[i][1]
        end = corner_points[(i + 1) % n][0]
        if not (
            math.isclose(start[0], end[0], abs_tol=1e-9)
            and math.isclose(start[1], end[1], abs_tol=1e-9)
        ):
            segments.append(_LineSeg(start=start, end=end))

    return segments


def apply_board_outline(
    pcb: "kicad.pcb.KicadPcb",
    vertices: list[OutlineVertex],
) -> None:
    """
    Replace the generated board outline on Edge.Cuts with the given polygon.

    Previously generated (uuid-marked) outline edges are removed; manually
    drawn Edge.Cuts geometry is preserved (and warned about, since the board
    would end up with two outlines).
    """
    from faebryk.exporters.pcb.kicad.transformer import PCB_Transformer

    segments = outline_segments(vertices)

    # drop previously generated outline edges
    for attr in ("gr_lines", "gr_arcs"):
        kicad.filter(
            pcb,
            attr,
            getattr(pcb, attr),
            lambda e: not (e.layer == EDGE_LAYER and PCB_Transformer.is_marked(e)),
        )

    manual_edges = [
        e
        for attr in ("gr_lines", "gr_arcs", "gr_rects", "gr_circles", "gr_polys")
        for e in getattr(pcb, attr)
        if e.layer == EDGE_LAYER
    ]
    if manual_edges:
        logger.warning(
            f"Board has {len(manual_edges)} hand-drawn Edge.Cuts elements in"
            " addition to the generated outline; remove one of the two"
        )

    stroke = lambda: kicad.pcb.Stroke(width=EDGE_STROKE_WIDTH, type="solid")  # noqa: E731

    for seg in segments:
        if isinstance(seg, _LineSeg):
            kicad.insert(
                pcb,
                "gr_lines",
                pcb.gr_lines,
                kicad.pcb.Line(
                    start=kicad.pcb.Xy(x=round(seg.start[0], 4), y=round(seg.start[1], 4)),
                    end=kicad.pcb.Xy(x=round(seg.end[0], 4), y=round(seg.end[1], 4)),
                    layer=EDGE_LAYER,
                    stroke=stroke(),
                    uuid=str(PCB_Transformer.gen_uuid(mark=True)),
                ),
            )
        else:
            kicad.insert(
                pcb,
                "gr_arcs",
                pcb.gr_arcs,
                kicad.pcb.Arc(
                    start=kicad.pcb.Xy(x=round(seg.start[0], 4), y=round(seg.start[1], 4)),
                    mid=kicad.pcb.Xy(x=round(seg.mid[0], 4), y=round(seg.mid[1], 4)),
                    end=kicad.pcb.Xy(x=round(seg.end[0], 4), y=round(seg.end[1], 4)),
                    layer=EDGE_LAYER,
                    stroke=stroke(),
                    uuid=str(PCB_Transformer.gen_uuid(mark=True)),
                ),
            )

    logger.info(
        f"Applied board outline ({len(segments)} edges,"
        f" {len(vertices)} vertices)"
    )
