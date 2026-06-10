# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""
Rule-based KiCad schematic writer.

Layout philosophy (v2):
- one sheet per *large* ato module; small modules are inlined into the parent
  sheet as visually grouped, titled clusters (mirroring how hand-drawn
  schematics group functional blocks on shared sheets)
- within a cluster, two-pin satellites are attached to the anchor pin they
  serve (input caps at VIN, feedback divider at FB, ...) with real wires, so
  local topology reads like the datasheet application circuit
- remaining connectivity uses net labels (global when a net spans sheets)
  and power flags, never long routed wires
- all UUIDs are uuid5-derived from stable keys: regenerated schematics are
  reproducible and diffable; symbols carry their atopile_address for a
  future position-sync layer
"""

import logging
import math
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from faebryk.exporters.schematic.from_pcb import (
    ComponentIR,
    SchematicIR,
    SheetIR,
    SymbolDef,
)
from faebryk.libs.kicad.sexp_tools import dump_sexp

logger = logging.getLogger(__name__)

GRID = 1.27
FORMAT_VERSION = 20231120  # KiCad 8 schematic format; readable by 9/10

_NS = uuid.uuid5(uuid.NAMESPACE_URL, "atopile-schematic")

PAPERS = {
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
}
MARGIN = 20.0
ROW_GAP = 10.16
COL_GAP = 10.16
STUB = 2.54
SAT_COL_GAP = 11.43  # anchor edge -> satellite column
SAT_SLOT = 10.16  # vertical pitch of satellite slots


def _uid(*key: str) -> str:
    return str(uuid.uuid5(_NS, ":".join(key)))


def _snap(v: float) -> float:
    return round(round(v / GRID) * GRID, 2)


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _rot_vec(vx: float, vy: float, rot: int) -> tuple[float, float]:
    """Rotate a sheet-space vector by the instance rotation (CCW on screen)."""
    a = math.radians(rot)
    return (
        round(vx * math.cos(a) + vy * math.sin(a), 4),
        round(-vx * math.sin(a) + vy * math.cos(a), 4),
    )


def _pin_offset(pin, rot: int) -> tuple[float, float]:
    """Sheet-space offset of a pin's connection point from the instance at."""
    return _rot_vec(pin.x, -pin.y, rot)


def _pin_outward(pin, rot: int) -> tuple[float, float]:
    """Sheet-space unit vector pointing away from the symbol body."""
    a = math.radians(pin.angle)
    return _rot_vec(-math.cos(a), math.sin(a), rot)


def _bbox_at_rot(
    bbox: tuple[float, float, float, float], rot: int
) -> tuple[float, float, float, float]:
    """Symbol bbox (y-up) -> sheet-space (left, top, right, bottom) extents."""
    x1, y1, x2, y2 = bbox
    pts = [
        _rot_vec(x, -y, rot)
        for x in (x1, x2)
        for y in (y1, y2)
    ]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


@dataclass
class _Placement:
    comp: ComponentIR
    rel: tuple[float, float]
    rot: int
    #: pin numbers terminated by an explicit wire (skip label/flag)
    wired_pins: set[str] = field(default_factory=set)


@dataclass
class _ClusterPlan:
    name: str
    placements: list[_Placement] = field(default_factory=list)
    #: relative wire polylines [(x, y), ...]
    wires: list[list[tuple[float, float]]] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0


class SheetWriter:
    def __init__(
        self,
        ir: SchematicIR,
        sheet: SheetIR,
        *,
        project: str,
        instance_path: str,
        file_name: str,
        child_files: dict[str, str],
        subtrees: dict[str, set[str]],
    ) -> None:
        self.ir = ir
        self.sheet = sheet
        self.project = project
        self.instance_path = instance_path
        self.file_name = file_name
        self.child_files = child_files
        self.subtrees = subtrees

        self.body: list[str] = []
        self.used_lib_ids: set[str] = set()
        self.used_power_nets: set[str] = set()
        self.pwr_counter = 0
        self.cursor_y = MARGIN + 8.0
        self.max_x = MARGIN

    # ------------------------------------------------------------------
    # cluster planning
    # ------------------------------------------------------------------

    def _label_room(self, comp: ComponentIR, pin, rot: int) -> float:
        net = comp.pin_nets.get(pin.number)
        if net is None or net not in self.ir.nets:
            return 3.0
        net_ir = self.ir.nets[net]
        ox, oy = _pin_outward(pin, rot)
        if net_ir.is_power and abs(oy) > 0.5 and ((oy > 0) == net_ir.is_gnd):
            return 9.0  # power flag
        return len(net) * 1.1 + STUB + 4.0

    def _free_extents(
        self, comp: ComponentIR, rot: int, wired: set[str]
    ) -> tuple[float, float, float, float]:
        """(left, top, right, bottom) room needed around the instance at."""
        sym = self.ir.symbols[comp.lib_id]
        l, t, r, b = _bbox_at_rot(sym.bbox, rot)
        left, top, right, bottom = -l, -t, r, b
        for pin in sym.pins:
            if pin.number in wired:
                continue
            room = self._label_room(comp, pin, rot)
            px, py = _pin_offset(pin, rot)
            ox, oy = _pin_outward(pin, rot)
            net = comp.pin_nets.get(pin.number)
            flagged = (
                net is not None
                and net in self.ir.nets
                and self.ir.nets[net].is_power
                and abs(oy) > 0.5
                and ((oy > 0) == self.ir.nets[net].is_gnd)
            )
            if flagged:
                half_text = len(net) * 0.65
                left = max(left, -px + half_text)
                right = max(right, px + half_text)
                if oy > 0:
                    bottom = max(bottom, py + room)
                else:
                    top = max(top, -py + room)
                continue
            if ox < -0.5:
                left = max(left, -px + room)
            elif ox > 0.5:
                right = max(right, px + room)
            elif oy > 0.5:
                bottom = max(bottom, py + room)
            else:
                top = max(top, -py + room)
        return left, top, right, bottom

    @staticmethod
    def _facing_rot(sym: SymbolDef, pin_number: str, desired: tuple[float, float]) -> int:
        """Rotation making `pin_number` point in `desired` sheet direction."""
        pin = next(p for p in sym.pins if p.number == pin_number)
        best, best_dot = 0, -2.0
        for rot in (0, 90, 180, 270):
            ox, oy = _pin_outward(pin, rot)
            dot = ox * desired[0] + oy * desired[1]
            if dot > best_dot:
                best, best_dot = rot, dot
        return best

    def _plan_cluster(self, name: str, comps: list[ComponentIR]) -> _ClusterPlan:
        plan = _ClusterPlan(name=name)
        anchors = [c for c in comps if c.role == "anchor"]
        satellites = [c for c in comps if c.role != "anchor"]

        # associate satellites to (anchor, pin): prefer a shared non-power net
        assoc: dict[str, list[tuple[ComponentIR, str]]] = {}  # anchor addr
        loose: list[ComponentIR] = []
        for sat in satellites:
            sat_nets = {
                n for n in sat.pin_nets.values() if n in self.ir.nets
            }
            best: tuple[int, str, str] | None = None  # (score, anchor, pin)
            for anchor in anchors:
                a_sym = self.ir.symbols[anchor.lib_id]
                for pin in a_sym.pins:
                    net = anchor.pin_nets.get(pin.number)
                    if net is None or net not in sat_nets:
                        continue
                    # prefer the functional association: signal nets bind
                    # strongest, then power rails; GND is meaningless as an
                    # anchor relation (everything touches it)
                    net_ir = self.ir.nets[net]
                    if net_ir.is_gnd:
                        score = 1
                    elif net_ir.is_power:
                        score = 2
                    else:
                        score = 3
                    if best is None or score > best[0]:
                        best = (score, anchor.address, pin.number)
            if best is None or best[0] <= 1:
                loose.append(sat)
            else:
                assoc.setdefault(best[1], []).append((sat, best[2]))

        # lay out anchors left to right, satellites flanking them
        x = 0.0
        height = 0.0
        for anchor in anchors:
            block_w, block_h = self._plan_anchor_block(
                plan, anchor, assoc.get(anchor.address, []), x
            )
            x += block_w + COL_GAP
            height = max(height, block_h)

        # loose satellites in a row underneath
        if loose:
            sx = 0.0
            row_h = 0.0
            for sat in loose:
                l, t, r, b = self._free_extents(sat, 0, set())
                plan.placements.append(
                    _Placement(comp=sat, rel=(sx + l, height + ROW_GAP + t), rot=0)
                )
                sx += l + r + COL_GAP * 0.7
                row_h = max(row_h, t + b)
            height += ROW_GAP + row_h
            x = max(x, sx)

        plan.width = max(x, 10.0)
        plan.height = max(height, 10.0)
        return plan

    def _plan_anchor_block(
        self,
        plan: _ClusterPlan,
        anchor: ComponentIR,
        sats: list[tuple[ComponentIR, str]],
        x0: float,
    ) -> tuple[float, float]:
        sym = self.ir.symbols[anchor.lib_id]
        wired_anchor_pins: set[str] = set()

        # split satellites by which side their anchor pin exits
        sides: dict[str, list[tuple[ComponentIR, str]]] = {"L": [], "R": []}
        for sat, pin_no in sats:
            pin = next(p for p in sym.pins if p.number == pin_no)
            ox, oy = _pin_outward(pin, 0)
            sides["L" if ox < 0 else "R"].append((sat, pin_no))

        a_l, a_t, a_r, a_b = self._free_extents(anchor, 0, set())

        # column widths
        col_extents: dict[str, float] = {}
        for side, items in sides.items():
            w = 0.0
            for sat, pin_no in items:
                desired = (1.0, 0.0) if side == "L" else (-1.0, 0.0)
                facing = self._sat_facing_pin(sat, anchor, pin_no)
                rot = self._facing_rot(
                    self.ir.symbols[sat.lib_id], facing, desired
                )
                l, t, r, b = self._free_extents(sat, rot, {facing})
                w = max(w, l + r)
            col_extents[side] = w

        left_col = col_extents["L"]
        right_col = col_extents["R"]

        anchor_x = x0 + (left_col + SAT_COL_GAP if sides["L"] else 0) + a_l
        anchor_y = a_t
        plan.placements.append(
            _Placement(
                comp=anchor,
                rel=(anchor_x, anchor_y),
                rot=0,
                wired_pins=wired_anchor_pins,
            )
        )
        anchor_placement = plan.placements[-1]

        total_h = a_t + a_b

        for side in ("L", "R"):
            items = sides[side]
            if not items:
                continue
            desired = (1.0, 0.0) if side == "L" else (-1.0, 0.0)
            slot_y = 0.0
            for idx, (sat, pin_no) in enumerate(items):
                pin = next(p for p in sym.pins if p.number == pin_no)
                facing = self._sat_facing_pin(sat, anchor, pin_no)
                s_sym = self.ir.symbols[sat.lib_id]
                rot = self._facing_rot(s_sym, facing, desired)
                s_l, s_t, s_r, s_b = self._free_extents(sat, rot, {facing})

                apx, apy = _pin_offset(pin, 0)
                conn_a = (anchor_x + apx, anchor_y + apy)

                # try to align with the anchor pin; otherwise stack downward
                target_y = max(conn_a[1], slot_y + s_t)
                slot_y = target_y + s_b + 2.54

                f_pin = next(p for p in s_sym.pins if p.number == facing)
                fpx, fpy = _pin_offset(f_pin, rot)

                if side == "L":
                    conn_s_x = anchor_x - a_l - SAT_COL_GAP + s_r * 0 + 0.0
                    conn_s_x = _snap(anchor_x - a_l - SAT_COL_GAP)
                else:
                    conn_s_x = _snap(anchor_x + a_r + SAT_COL_GAP)
                conn_s = (conn_s_x, _snap(target_y))

                sat_at = (conn_s[0] - fpx, conn_s[1] - fpy)
                plan.placements.append(
                    _Placement(
                        comp=sat, rel=sat_at, rot=rot, wired_pins={facing}
                    )
                )
                wired_anchor_pins.add(pin_no)

                # wire anchor pin -> satellite pin (H-V-H); vertical runs are
                # distributed across the gap between column and anchor so
                # parallel wires don't overlap
                channel = SAT_COL_GAP - 2.54
                step = max(GRID, channel / max(len(items), 1))
                offset = 2.54 + (idx % max(len(items), 1)) * step
                bend_x = _snap(
                    conn_s[0] + offset if side == "L" else conn_s[0] - offset
                )
                if abs(conn_a[1] - conn_s[1]) < 0.01:
                    plan.wires.append([conn_a, conn_s])
                else:
                    plan.wires.append(
                        [
                            conn_a,
                            (bend_x, conn_a[1]),
                            (bend_x, conn_s[1]),
                            conn_s,
                        ]
                    )
                total_h = max(total_h, target_y + s_b)

        width = (
            (left_col + SAT_COL_GAP if sides["L"] else 0)
            + a_l
            + a_r
            + (SAT_COL_GAP + right_col if sides["R"] else 0)
        )
        return width, total_h

    def _sat_facing_pin(
        self, sat: ComponentIR, anchor: ComponentIR, anchor_pin: str
    ) -> str:
        """The satellite pin on the same net as the anchor pin."""
        net = anchor.pin_nets.get(anchor_pin)
        for number, n in sat.pin_nets.items():
            if n == net:
                return number
        return next(iter(sat.pin_nets))

    # ------------------------------------------------------------------
    # emission
    # ------------------------------------------------------------------

    def _wire_abs(self, points: list[tuple[float, float]], key: str):
        for i, (a, b) in enumerate(zip(points, points[1:])):
            if abs(a[0] - b[0]) < 0.01 and abs(a[1] - b[1]) < 0.01:
                continue
            self.body.append(
                f"\t(wire (pts (xy {a[0]:g} {a[1]:g}) (xy {b[0]:g} {b[1]:g}))\n"
                f"\t\t(stroke (width 0) (type default))\n"
                f'\t\t(uuid "{_uid(self.file_name, "wire", key, str(i))}")\n'
                f"\t)"
            )

    def _crosses_boundary(self, net: str) -> bool:
        """True if the net has members outside this sheet's subtree."""
        subtree = self.subtrees[self.sheet.path]
        return any(s not in subtree for s in self.ir.nets[net].sheets)

    def _label(self, net: str, at: tuple[float, float], angle: int, key: str):
        net_ir = self.ir.nets[net]
        if net_ir.is_power:
            # power nets connect via (global) power flags everywhere; a
            # sideways power pin still needs the global scope to join them
            kind, shape = "global_label", " (shape passive)"
        elif self.sheet.path != "" and self._crosses_boundary(net):
            # signal leaving this sheet: hierarchical port, matched by a pin
            # on the sheet symbol in the parent
            kind, shape = "hierarchical_label", " (shape passive)"
        else:
            kind, shape = "label", ""
        justify = {0: "left", 180: "right", 90: "left", 270: "right"}[angle]
        self.body.append(
            f'\t({kind} "{_esc(net)}"{shape} (at {at[0]:g} {at[1]:g} {angle})\n'
            f"\t\t(effects (font (size 1.27 1.27)) (justify {justify}))\n"
            f'\t\t(uuid "{_uid(self.file_name, kind, key)}")\n'
            f"\t)"
        )

    def _power_symbol(self, net: str, at: tuple[float, float], key: str):
        self.used_power_nets.add(net)
        self.pwr_counter += 1
        ref = f"#PWR{self.pwr_counter:03d}"
        net_ir = self.ir.nets[net]
        lib = f"atopile:PWR_{_power_sym_name(net)}"
        value_y = at[1] + 5.6 if net_ir.is_gnd else at[1] - 3.6
        self.body.append(
            f'\t(symbol (lib_id "{lib}") (at {at[0]:g} {at[1]:g} 0) (unit 1)\n'
            f"\t\t(exclude_from_sim no) (in_bom no) (on_board yes) (dnp no)\n"
            f'\t\t(uuid "{_uid(self.file_name, "pwr", key)}")\n'
            f'\t\t(property "Reference" "{ref}" (at {at[0]:g} {at[1]:g} 0)\n'
            f"\t\t\t(effects (font (size 1.27 1.27)) hide)\n"
            f"\t\t)\n"
            f'\t\t(property "Value" "{_esc(net)}"'
            f" (at {at[0]:g} {value_y:g} 0)\n"
            f"\t\t\t(effects (font (size 1.27 1.27)))\n"
            f"\t\t)\n"
            f'\t\t(pin "1" (uuid "{_uid(self.file_name, "pwrpin", key)}"))\n'
            f'\t\t(instances (project "{_esc(self.project)}"\n'
            f'\t\t\t(path "{self.instance_path}" (reference "{ref}") (unit 1))\n'
            f"\t\t))\n"
            f"\t)"
        )

    def _pin_termination(self, placement: _Placement, pin, at: tuple[float, float]):
        comp = placement.comp
        if pin.number in placement.wired_pins:
            return
        net_name = comp.pin_nets.get(pin.number)
        key = f"{comp.address}:{pin.number}"
        px, py = _pin_offset(pin, placement.rot)
        conn = (_snap(at[0] + px), _snap(at[1] + py))

        if net_name is None or net_name not in self.ir.nets:
            self.body.append(
                f"\t(no_connect (at {conn[0]:g} {conn[1]:g})"
                f' (uuid "{_uid(self.file_name, "nc", key)}"))'
            )
            return

        net = self.ir.nets[net_name]
        ox, oy = _pin_outward(pin, placement.rot)

        if net.is_power and abs(oy) > 0.5 and ((oy > 0) == net.is_gnd):
            end = (conn[0], conn[1] + (STUB if net.is_gnd else -STUB) * 2)
            self._wire_abs([conn, end], key)
            self._power_symbol(net_name, end, key)
            return

        stub = STUB * 1.5
        end = (_snap(conn[0] + ox * stub), _snap(conn[1] + oy * stub))
        self._wire_abs([conn, end], key)
        if abs(ox) > 0.5:
            angle = 0 if ox > 0 else 180
        else:
            angle = 270 if oy > 0 else 90
        self._label(net_name, end, angle, key)

    def _instance(self, placement: _Placement, at: tuple[float, float]):
        comp = placement.comp
        sym = self.ir.symbols[comp.lib_id]
        self.used_lib_ids.add(comp.lib_id)
        l, t, r, b = _bbox_at_rot(sym.bbox, placement.rot)
        ref_pos = (at[0] + l, at[1] + t - 2.0)
        val_pos = (at[0] + l, at[1] + b + 2.0)

        pin_lines = "".join(
            f'\t\t(pin "{p.number}"'
            f' (uuid "{_uid(self.file_name, "pin", comp.address, p.number)}"))\n'
            for p in sym.pins
        )
        self.body.append(
            f'\t(symbol (lib_id "{comp.lib_id}")'
            f" (at {at[0]:g} {at[1]:g} {placement.rot}) (unit 1)\n"
            f"\t\t(exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)\n"
            f'\t\t(uuid "{_uid(self.file_name, "sym", comp.address)}")\n'
            f'\t\t(property "Reference" "{_esc(comp.reference)}"'
            f" (at {ref_pos[0]:g} {ref_pos[1]:g} 0)\n"
            f"\t\t\t(effects (font (size 1.27 1.27)) (justify left))\n"
            f"\t\t)\n"
            f'\t\t(property "Value" "{_esc(comp.value)}"'
            f" (at {val_pos[0]:g} {val_pos[1]:g} 0)\n"
            f"\t\t\t(effects (font (size 1.27 1.27)) (justify left))\n"
            f"\t\t)\n"
            f'\t\t(property "Footprint" "" (at {at[0]:g} {at[1]:g} 0)\n'
            f"\t\t\t(effects (font (size 1.27 1.27)) hide)\n"
            f"\t\t)\n"
            f'\t\t(property "atopile_address" "{_esc(comp.address)}"'
            f" (at {at[0]:g} {at[1]:g} 0)\n"
            f"\t\t\t(effects (font (size 1.27 1.27)) hide)\n"
            f"\t\t)\n"
            f"{pin_lines}"
            f'\t\t(instances (project "{_esc(self.project)}"\n'
            f'\t\t\t(path "{self.instance_path}"'
            f' (reference "{_esc(comp.reference)}") (unit 1))\n'
            f"\t\t))\n"
            f"\t)"
        )
        for pin in sym.pins:
            self._pin_termination(placement, pin, at)

    def _emit_cluster(self, plan: _ClusterPlan, origin: tuple[float, float]):
        ox, oy = origin
        if plan.name:
            self.body.append(
                f'\t(text "{_esc(plan.name)}"'
                f" (at {ox:g} {oy - 2.5:g} 0)\n"
                f"\t\t(effects (font (size 2 2) bold) (justify left bottom))\n"
                f'\t\t(uuid "{_uid(self.file_name, "ctitle", plan.name)}")\n'
                f"\t)"
            )
        for placement in plan.placements:
            at = (_snap(ox + placement.rel[0]), _snap(oy + placement.rel[1]))
            self._instance(placement, at)
        for i, wire in enumerate(plan.wires):
            pts = [(_snap(ox + x), _snap(oy + y)) for x, y in wire]
            self._wire_abs(pts, f"cw:{plan.name}:{i}")

    def _child_ports(self, child: SheetIR) -> list[str]:
        """Signal nets crossing the child sheet's boundary (its ports)."""
        subtree = self.subtrees[child.path]
        ports = [
            net.name
            for net in self.ir.nets.values()
            if not net.is_power
            and any(s in subtree for s in net.sheets)
            and any(s not in subtree for s in net.sheets)
        ]
        return sorted(ports)

    def _place_sheet_boxes(self, usable_width: float):
        if not self.sheet.children:
            return
        x = MARGIN
        row_h = 0.0
        w = 60.0
        for child in self.sheet.children:
            ports = self._child_ports(child)
            h = _snap(max(16.0, (len(ports) + 3) * 2.54))
            # room on the left of the box for the port stubs + labels
            label_room = max(
                [len(p) * 1.1 + 8.0 for p in ports], default=4.0
            )
            if x + label_room + w > usable_width and row_h:
                x = MARGIN
                self.cursor_y += row_h + ROW_GAP
                row_h = 0.0
            sheet_uuid = _uid("sheetel", child.path)
            at = (_snap(x + label_room), _snap(self.cursor_y))
            file_name = self.child_files[child.path]

            pin_lines = ""
            for i, port in enumerate(ports):
                py = _snap(at[1] + 2.54 * (i + 2))
                pin_lines += (
                    f'\t\t(pin "{_esc(port)}" passive (at {at[0]:g} {py:g} 180)\n'
                    f"\t\t\t(effects (font (size 1.27 1.27)) (justify left))\n"
                    f'\t\t\t(uuid "{_uid("sheetpin", child.path, port)}")\n'
                    f"\t\t)\n"
                )

            self.body.append(
                f"\t(sheet (at {at[0]:g} {at[1]:g}) (size {w:g} {h:g})\n"
                f"\t\t(stroke (width 0.1524) (type solid))"
                f" (fill (color 0 0 0 0.0000))\n"
                f'\t\t(uuid "{sheet_uuid}")\n'
                f'\t\t(property "Sheetname" "{_esc(child.name)}"'
                f" (at {at[0]:g} {at[1] - 1:g} 0)\n"
                f"\t\t\t(effects (font (size 1.27 1.27)) (justify left bottom))\n"
                f"\t\t)\n"
                f'\t\t(property "Sheetfile" "{_esc(file_name)}"'
                f" (at {at[0]:g} {at[1] + h + 1:g} 0)\n"
                f"\t\t\t(effects (font (size 1.27 1.27)) (justify left top))\n"
                f"\t\t)\n"
                f"{pin_lines}"
                f'\t\t(instances (project "{_esc(self.project)}"\n'
                f'\t\t\t(path "{self.instance_path}" (page "?"))\n'
                f"\t\t))\n"
                f"\t)"
            )
            # parent-side termination of each port: stub wire + label
            for i, port in enumerate(ports):
                py = _snap(at[1] + 2.54 * (i + 2))
                end = (_snap(at[0] - STUB * 1.5), py)
                self._wire_abs([(at[0], py), end], f"sp:{child.path}:{port}")
                self._label(port, end, 180, f"sp:{child.path}:{port}")

            x += label_room + w + COL_GAP
            self.max_x = max(self.max_x, x)
            row_h = max(row_h, h)
        self.cursor_y += row_h + ROW_GAP * 1.5

    def render(self) -> str:
        usable_width = PAPERS["A3"][0] - 2 * MARGIN

        self._place_sheet_boxes(usable_width)

        # group by cluster; the sheet's own components ("") come first
        clusters: dict[str, list[ComponentIR]] = {}
        for comp in self.sheet.components:
            clusters.setdefault(comp.cluster, []).append(comp)

        plans = [
            self._plan_cluster(name, comps)
            for name, comps in sorted(
                clusters.items(), key=lambda kv: (kv[0] != "", kv[0])
            )
        ]

        # pack cluster blocks in rows
        x = MARGIN
        row_h = 0.0
        row_started = False
        for plan in plans:
            title_room = 6.0 if plan.name else 0.0
            if row_started and x + plan.width > usable_width:
                self.cursor_y += row_h + ROW_GAP * 1.5
                x = MARGIN
                row_h = 0.0
                row_started = False
            self._emit_cluster(plan, (x, self.cursor_y + title_room))
            x += plan.width + COL_GAP * 1.5
            self.max_x = max(self.max_x, x)
            row_h = max(row_h, plan.height + title_room)
            row_started = True
        if row_started:
            self.cursor_y += row_h + ROW_GAP

        paper = "A1"
        for name in ("A4", "A3", "A2", "A1"):
            w, h = PAPERS[name]
            if self.max_x + MARGIN <= w and self.cursor_y + MARGIN <= h:
                paper = name
                break

        lib_symbols = "".join(
            "\t"
            + dump_sexp(self.ir.symbols[lib_id].node, 1).replace(
                f'(symbol "{self.ir.symbols[lib_id].identifier}"',
                f'(symbol "{lib_id}"',
                1,
            )
            + "\n"
            for lib_id in sorted(self.used_lib_ids)
        )
        lib_symbols += "".join(
            _power_symbol_lib(net, self.ir.nets[net].is_gnd)
            for net in sorted(self.used_power_nets)
        )

        sheet_instances = ""
        if self.instance_path.count("/") == 1 and self.sheet.path == "":
            sheet_instances = '\t(sheet_instances (path "/" (page "1")))\n'

        return (
            f"(kicad_sch\n"
            f"\t(version {FORMAT_VERSION})\n"
            f'\t(generator "atopile")\n'
            f'\t(uuid "{_uid("file", self.file_name)}")\n'
            f'\t(paper "{paper}")\n'
            f"\t(title_block\n"
            f'\t\t(title "{_esc(self.sheet.name)}")\n'
            f'\t\t(comment 1 "generated by atopile — do not treat as source")\n'
            f"\t)\n"
            f"\t(lib_symbols\n{lib_symbols}\t)\n"
            + "\n".join(self.body)
            + "\n"
            + sheet_instances
            + ")\n"
        )


def _power_sym_name(net: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in net)


def _power_symbol_lib(net: str, is_gnd: bool) -> str:
    name = f"atopile:PWR_{_power_sym_name(net)}"
    if is_gnd:
        graphics = (
            "\t\t\t(polyline (pts (xy 0 0) (xy 0 -1.27))"
            " (stroke (width 0) (type default)) (fill (type none)))\n"
            "\t\t\t(polyline (pts (xy -1.27 -1.27) (xy 1.27 -1.27))"
            " (stroke (width 0) (type default)) (fill (type none)))\n"
            "\t\t\t(polyline (pts (xy -0.762 -1.778) (xy 0.762 -1.778))"
            " (stroke (width 0) (type default)) (fill (type none)))\n"
            "\t\t\t(polyline (pts (xy -0.254 -2.286) (xy 0.254 -2.286))"
            " (stroke (width 0) (type default)) (fill (type none)))\n"
        )
    else:
        graphics = (
            "\t\t\t(polyline (pts (xy 0 0) (xy 0 1.27))"
            " (stroke (width 0) (type default)) (fill (type none)))\n"
            "\t\t\t(polyline (pts (xy -1.016 1.27) (xy 1.016 1.27))"
            " (stroke (width 0.254) (type default)) (fill (type none)))\n"
        )
    return (
        f'\t(symbol "{name}" (power) (pin_names (offset 0))'
        f" (exclude_from_sim yes) (in_bom no) (on_board yes)\n"
        f'\t\t(property "Reference" "#PWR" (at 0 0 0)\n'
        f"\t\t\t(effects (font (size 1.27 1.27)) hide)\n"
        f"\t\t)\n"
        f'\t\t(property "Value" "{_esc(net)}" (at 0 0 0)\n'
        f"\t\t\t(effects (font (size 1.27 1.27)))\n"
        f"\t\t)\n"
        f'\t\t(symbol "PWR_{_power_sym_name(net)}_0_1"\n'
        f"{graphics}"
        f"\t\t)\n"
        f'\t\t(symbol "PWR_{_power_sym_name(net)}_1_1"\n'
        f"\t\t\t(pin power_in line (at 0 0 90) (length 0) hide\n"
        f'\t\t\t\t(name "{_esc(net)}" (effects (font (size 1.27 1.27))))\n'
        f'\t\t\t\t(number "1" (effects (font (size 1.27 1.27))))\n'
        f"\t\t\t)\n"
        f"\t\t)\n"
        f"\t)\n"
    )


def write_schematic(
    ir: SchematicIR, out_dir: Path, *, target_name: str
) -> list[Path]:
    """Write the root + child sheet .kicad_sch files; returns written paths."""
    out_dir.mkdir(parents=True, exist_ok=True)

    files: dict[str, str] = {"": f"{target_name}.kicad_sch"}

    def collect(sheet: SheetIR):
        for child in sheet.children:
            files[child.path] = f"{target_name}.{child.path}.kicad_sch"
            collect(child)

    collect(ir.root)

    # sheet path -> set of paths in its subtree (inclusive)
    subtrees: dict[str, set[str]] = {}

    def collect_subtree(sheet: SheetIR) -> set[str]:
        paths = {sheet.path}
        for child in sheet.children:
            paths |= collect_subtree(child)
        subtrees[sheet.path] = paths
        return paths

    collect_subtree(ir.root)

    root_uuid = _uid("file", files[""])
    written: list[Path] = []

    def emit(sheet: SheetIR, instance_path: str):
        writer = SheetWriter(
            ir,
            sheet,
            project=target_name,
            instance_path=instance_path,
            file_name=files[sheet.path],
            child_files=files,
            subtrees=subtrees,
        )
        content = writer.render()
        path = out_dir / files[sheet.path]
        path.write_text(content, encoding="utf-8")
        written.append(path)
        for child in sheet.children:
            sheet_el_uuid = _uid("sheetel", child.path)
            emit(child, f"{instance_path}/{sheet_el_uuid}")

    emit(ir.root, f"/{root_uuid}")
    return written
