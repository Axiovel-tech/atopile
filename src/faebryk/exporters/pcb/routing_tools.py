# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""
Programmatic routing primitives on saved `.kicad_pcb` files.

These operate purely on the board file (nets resolved by name from the net
table), so they can be driven from the CLI / by agents without a design
graph: report the ratsnest, lay tracks and vias, create zones, then validate
with `kicad-cli pcb drc`.
"""

import logging
import math
from dataclasses import dataclass, field

from faebryk.libs.kicad.fileformats import kicad

logger = logging.getLogger(__name__)


class RoutingError(Exception):
    pass


def net_table(pcb: "kicad.pcb.KicadPcb") -> dict[str, int]:
    return {n.name: n.number for n in pcb.nets if n.name is not None}


def _net_number(pcb: "kicad.pcb.KicadPcb", net: str) -> int:
    table = net_table(pcb)
    if net not in table:
        candidates = ", ".join(sorted(table)[:40])
        raise RoutingError(f"Net `{net}` not found. Nets: {candidates} ...")
    return table[net]


def copper_layers(pcb: "kicad.pcb.KicadPcb") -> list[str]:
    return [
        layer.name
        for layer in pcb.layers
        if layer.name.endswith(".Cu")
    ]


def _check_layer(pcb: "kicad.pcb.KicadPcb", layer: str) -> None:
    if layer not in copper_layers(pcb):
        raise RoutingError(
            f"Layer `{layer}` is not a copper layer of this board"
            f" ({', '.join(copper_layers(pcb))})"
        )


def add_track(
    pcb: "kicad.pcb.KicadPcb",
    net: str,
    points: list[tuple[float, float]],
    width: float,
    layer: str,
) -> int:
    """Add a polyline track; returns the number of segments created."""
    from faebryk.exporters.pcb.kicad.transformer import PCB_Transformer

    if len(points) < 2:
        raise RoutingError("Track needs at least 2 points")
    _check_layer(pcb, layer)
    number = _net_number(pcb, net)

    count = 0
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        if math.isclose(x1, x2, abs_tol=1e-9) and math.isclose(
            y1, y2, abs_tol=1e-9
        ):
            continue
        kicad.insert(
            pcb,
            "segments",
            pcb.segments,
            kicad.pcb.Segment(
                start=kicad.pcb.Xy(x=x1, y=y1),
                end=kicad.pcb.Xy(x=x2, y=y2),
                width=width,
                layer=layer,
                net=number,
                uuid=str(PCB_Transformer.gen_uuid(mark=True)),
            ),
        )
        count += 1
    return count


def add_via(
    pcb: "kicad.pcb.KicadPcb",
    net: str,
    at: tuple[float, float],
    size: float = 0.47,
    drill: float = 0.25,
) -> None:
    from faebryk.exporters.pcb.kicad.transformer import PCB_Transformer

    number = _net_number(pcb, net)
    kicad.insert(
        pcb,
        "vias",
        pcb.vias,
        kicad.pcb.Via(
            at=kicad.pcb.Xy(x=at[0], y=at[1]),
            size=size,
            drill=drill,
            layers=["F.Cu", "B.Cu"],
            net=number,
            uuid=str(PCB_Transformer.gen_uuid(mark=True)),
        ),
    )


def board_bbox(
    pcb: "kicad.pcb.KicadPcb",
) -> tuple[float, float, float, float] | None:
    """Bounding box (x1, y1, x2, y2) of the Edge.Cuts outline."""
    xs: list[float] = []
    ys: list[float] = []
    for el in list(pcb.gr_lines) + list(pcb.gr_arcs):
        if el.layer != "Edge.Cuts":
            continue
        for pt_name in ("start", "mid", "end"):
            pt = getattr(el, pt_name, None)
            if pt is not None:
                xs.append(pt.x)
                ys.append(pt.y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def add_zone(
    pcb: "kicad.pcb.KicadPcb",
    net: str,
    layer: str,
    polygon: list[tuple[float, float]] | None = None,
    *,
    name: str | None = None,
    clearance: float = 0.2,
    min_thickness: float = 0.2,
    thermal_gap: float = 0.5,
    thermal_bridge_width: float = 0.5,
    priority: int | None = None,
) -> None:
    """
    Add a filled zone. With no polygon, the zone covers the board outline
    bounding box (typical for full power/ground planes).
    """
    from faebryk.exporters.pcb.kicad.transformer import PCB_Transformer

    _check_layer(pcb, layer)
    number = _net_number(pcb, net)

    if polygon is None:
        bbox = board_bbox(pcb)
        if bbox is None:
            raise RoutingError(
                "No board outline found; pass an explicit polygon"
            )
        x1, y1, x2, y2 = bbox
        polygon = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]

    pts = kicad.pcb.Pts(
        xys=[kicad.pcb.Xy(x=x, y=y) for x, y in polygon]
    )

    kicad.insert(
        pcb,
        "zones",
        pcb.zones,
        kicad.pcb.Zone(
            net=number,
            net_name=net,
            layer=layer,
            uuid=str(PCB_Transformer.gen_uuid(mark=True)),
            name=name,
            hatch=kicad.pcb.Hatch(mode="edge", pitch=0.5),
            priority=priority,
            connect_pads=kicad.pcb.ConnectPads(mode=None, clearance=clearance),
            min_thickness=min_thickness,
            filled_areas_thickness=False,
            fill=kicad.pcb.ZoneFill(
                enable="yes",
                thermal_gap=thermal_gap,
                thermal_bridge_width=thermal_bridge_width,
            ),
            polygon=kicad.pcb.Polygon(pts=pts),
        ),
    )


@dataclass
class NetRatsnest:
    net: str
    pads: list[tuple[str, float, float, str]] = field(default_factory=list)
    segments: int = 0
    vias: int = 0
    zones: int = 0


def ratsnest(pcb: "kicad.pcb.KicadPcb") -> list[NetRatsnest]:
    """Per-net connection points (pad positions) and existing copper counts."""
    by_net: dict[str, NetRatsnest] = {}

    def entry(net_name: str) -> NetRatsnest:
        if net_name not in by_net:
            by_net[net_name] = NetRatsnest(net=net_name)
        return by_net[net_name]

    for fp in pcb.footprints:
        ref = next(
            (p.value for p in fp.propertys if p.name == "Reference"), "?"
        )
        r = math.radians(fp.at.r or 0)
        for pad in fp.pads:
            if not pad.net or not pad.net.name:
                continue
            px = fp.at.x + pad.at.x * math.cos(r) + pad.at.y * math.sin(r)
            py = fp.at.y - pad.at.x * math.sin(r) + pad.at.y * math.cos(r)
            layers = list(pad.layers)
            cu = next((x for x in layers if x.endswith(".Cu")), "F.Cu")
            entry(pad.net.name).pads.append(
                (f"{ref}.{pad.name}", round(px, 3), round(py, 3), cu)
            )

    nets_by_number = {n.number: n.name for n in pcb.nets}
    for seg in pcb.segments:
        if (name := nets_by_number.get(seg.net)) is not None:
            entry(name).segments += 1
    for via in pcb.vias:
        if (name := nets_by_number.get(via.net)) is not None:
            entry(name).vias += 1
    for zone in pcb.zones:
        if zone.net_name:
            entry(zone.net_name).zones += 1

    return sorted(by_net.values(), key=lambda e: e.net)


def apply_route_plan(pcb: "kicad.pcb.KicadPcb", plan: dict) -> dict:
    """
    Apply a route plan:

    ```json
    {
      "tracks": [{"net": "GND", "layer": "F.Cu", "width": 0.3,
                  "points": [[x, y], [x, y], ...]}],
      "vias":   [{"net": "GND", "at": [x, y], "size": 0.47, "drill": 0.25}],
      "zones":  [{"net": "GND", "layer": "In1.Cu",
                  "polygon": [[x, y], ...] | null, "name": "L2_GND",
                  "clearance": 0.2, "min_thickness": 0.2, "priority": 0}]
    }
    ```

    Returns counts of created objects.
    """
    counts = {"segments": 0, "vias": 0, "zones": 0}
    for track in plan.get("tracks", []):
        counts["segments"] += add_track(
            pcb,
            net=track["net"],
            points=[tuple(p) for p in track["points"]],
            width=track.get("width", 0.25),
            layer=track["layer"],
        )
    for via in plan.get("vias", []):
        add_via(
            pcb,
            net=via["net"],
            at=tuple(via["at"]),
            size=via.get("size", 0.47),
            drill=via.get("drill", 0.25),
        )
        counts["vias"] += 1
    for zone in plan.get("zones", []):
        add_zone(
            pcb,
            net=zone["net"],
            layer=zone["layer"],
            polygon=[tuple(p) for p in zone["polygon"]]
            if zone.get("polygon")
            else None,
            name=zone.get("name"),
            clearance=zone.get("clearance", 0.2),
            min_thickness=zone.get("min_thickness", 0.2),
            priority=zone.get("priority"),
        )
        counts["zones"] += 1
    return counts


@dataclass
class _PadOnBoard:
    net: int
    x: float
    y: float
    half_w: float
    half_h: float
    layer: str
    ref: str
    name: str


def _all_pads(pcb: "kicad.pcb.KicadPcb") -> list[_PadOnBoard]:
    pads: list[_PadOnBoard] = []
    for fp in pcb.footprints:
        ref = next(
            (p.value for p in fp.propertys if p.name == "Reference"), "?"
        )
        r = math.radians(fp.at.r or 0)
        for pad in fp.pads:
            px = fp.at.x + pad.at.x * math.cos(r) + pad.at.y * math.sin(r)
            py = fp.at.y - pad.at.x * math.sin(r) + pad.at.y * math.cos(r)
            w = (pad.size.w if pad.size else 1) / 2
            h = (pad.size.h if pad.size else 1) / 2
            # pad rotation (in-file) is absolute; if rotated odd multiples of
            # 90, swap extents. Use bounding circle-ish approximation.
            pad_r = math.radians(pad.at.r or 0)
            if abs(math.sin(pad_r)) > 0.5:
                w, h = h, w
            cu = next((x for x in pad.layers if x.endswith(".Cu")), "F.Cu")
            pads.append(
                _PadOnBoard(
                    net=pad.net.number if pad.net else 0,
                    x=px,
                    y=py,
                    half_w=w,
                    half_h=h,
                    layer=cu,
                    ref=ref,
                    name=pad.name,
                )
            )
    return pads


def fanout_net(
    pcb: "kicad.pcb.KicadPcb",
    net: str,
    *,
    via_size: float = 0.47,
    drill: float = 0.25,
    clearance: float = 0.2,
    track_width: float = 0.3,
    share_radius: float = 1.5,
) -> dict:
    """
    Plane fanout: place a via next to every surface pad of `net` (plus a
    short connecting track), so the pads connect to inner-plane zones.

    Pads that already have a same-net via within `share_radius` share it.
    Via positions are chosen from 8 candidate directions around the pad,
    rejecting spots that collide with other-net pads, existing vias or the
    board edge. Returns counts and the pads that could not be fanned out.
    """
    number = _net_number(pcb, net)
    pads = _all_pads(pcb)
    via_r = via_size / 2

    existing_vias: list[tuple[float, float, int]] = [
        (v.at.x, v.at.y, v.net) for v in pcb.vias
    ]
    bbox = board_bbox(pcb)

    def via_ok(x: float, y: float) -> bool:
        if bbox is not None:
            x1, y1, x2, y2 = bbox
            margin = via_r + 0.3
            if not (
                x1 + margin <= x <= x2 - margin
                and y1 + margin <= y <= y2 - margin
            ):
                return False
        for p in pads:
            if p.net == number:
                continue
            # expand pad bbox by clearance + via radius
            if (
                abs(x - p.x) <= p.half_w + clearance + via_r
                and abs(y - p.y) <= p.half_h + clearance + via_r
            ):
                return False
        for vx, vy, vnet in existing_vias:
            min_dist = 2 * via_r + (0 if vnet == number else clearance)
            if math.hypot(x - vx, y - vy) < min_dist:
                return False
        return True

    counts = {"vias": 0, "segments": 0, "shared": 0, "failed": []}

    for p in [p for p in pads if p.net == number]:
        # already near a same-net via?
        if any(
            vnet == number and math.hypot(p.x - vx, p.y - vy) <= share_radius
            for vx, vy, vnet in existing_vias
        ):
            counts["shared"] += 1
            continue

        d_x = p.half_w + clearance + via_r
        d_y = p.half_h + clearance + via_r
        d_diag = max(d_x, d_y) * 1.05
        candidates = [
            (p.x, p.y + d_y),
            (p.x, p.y - d_y),
            (p.x + d_x, p.y),
            (p.x - d_x, p.y),
            (p.x + d_diag * 0.707, p.y + d_diag * 0.707),
            (p.x - d_diag * 0.707, p.y + d_diag * 0.707),
            (p.x + d_diag * 0.707, p.y - d_diag * 0.707),
            (p.x - d_diag * 0.707, p.y - d_diag * 0.707),
        ]
        placed = False
        for cx, cy in candidates:
            if not via_ok(cx, cy):
                continue
            add_via(pcb, net, (cx, cy), size=via_size, drill=drill)
            counts["segments"] += add_track(
                pcb, net, [(p.x, p.y), (cx, cy)], track_width, p.layer
            )
            existing_vias.append((cx, cy, number))
            counts["vias"] += 1
            placed = True
            break
        if not placed:
            counts["failed"].append(f"{p.ref}.{p.name}")

    return counts


def clear_generated_routing(pcb: "kicad.pcb.KicadPcb") -> dict:
    """Remove previously generated (uuid-marked) tracks, vias and zones."""
    from faebryk.exporters.pcb.kicad.transformer import PCB_Transformer

    counts = {"segments": 0, "vias": 0, "zones": 0}
    for attr in ("segments", "vias", "zones"):
        container = getattr(pcb, attr)
        before = len(container)
        kicad.filter(
            pcb, attr, container, lambda e: not PCB_Transformer.is_marked(e)
        )
        counts[attr] = before - len(getattr(pcb, attr))
    return counts
