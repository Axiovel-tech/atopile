# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""
Rule-based KiCad schematic writer.

Layout philosophy (v1):
- one sheet per (large enough) ato module; the module tree is the
  organization, mirrored as KiCad hierarchical sheets
- per sheet, components are grouped by role: anchors (ICs/connectors),
  series elements, pull up/downs, decoupling — each in its own row
- connectivity is expressed with net labels and power symbols instead of
  routed wires: every pin gets a short stub wire plus a label (local or
  global) or a power flag; this is deterministic and never degenerates
  into crossing-wire spaghetti
- all UUIDs are uuid5-derived from stable keys, so regenerated schematics
  are reproducible and diffable

Positions are deterministic; a future sync layer can preserve manual
adjustments keyed by the atopile_address property each symbol carries.
"""

import logging
import math
import uuid
from dataclasses import dataclass
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
COL_GAP = 7.62
STUB = 2.54


def _uid(*key: str) -> str:
    return str(uuid.uuid5(_NS, ":".join(key)))


def _snap(v: float) -> float:
    return round(round(v / GRID) * GRID, 2)


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


@dataclass
class _PlacedPin:
    number: str
    conn: tuple[float, float]  # sheet coords
    outward: tuple[float, float]  # unit vector away from body
    net: str | None


@dataclass
class _PlacedComponent:
    comp: ComponentIR
    sym: SymbolDef
    at: tuple[float, float]
    pins: list[_PlacedPin]


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
    ) -> None:
        self.ir = ir
        self.sheet = sheet
        self.project = project
        self.instance_path = instance_path
        self.file_name = file_name
        self.child_files = child_files

        self.body: list[str] = []
        self.used_lib_ids: set[str] = set()
        self.used_power_nets: set[str] = set()
        self.pwr_counter = 0
        self.cursor_y = MARGIN + 12.0
        self.max_x = MARGIN
        self.sheet_pages: list[tuple[str, str]] = []  # (sheet uuid, page)

    # -- geometry helpers --------------------------------------------------

    def _component_extents(
        self, comp: ComponentIR
    ) -> tuple[float, float, float, float]:
        """(left, right, up, down) space needed around the anchor point."""
        sym = self.ir.symbols[comp.lib_id]
        x1, y1, x2, y2 = sym.bbox
        left, right = -x1, x2
        up, down = y2, -y1

        for pin in sym.pins:
            net = comp.pin_nets.get(pin.number)
            if net is None or net not in self.ir.nets:
                continue
            label_len = len(net) * 1.1 + STUB + 4.0
            ox, oy = self._outward(pin)
            net_ir = self.ir.nets[net]
            vertical = abs(oy) > 0.5
            flagged = net_ir.is_power and vertical and (
                (oy > 0) == net_ir.is_gnd
            )
            if flagged:
                # power flag above/below: room for the flag + its value text
                half_text = len(net) * 0.65
                left = max(left, -pin.x + half_text)
                right = max(right, pin.x + half_text)
                if net_ir.is_gnd:
                    down = max(down, -pin.y + 8.0)
                else:
                    up = max(up, pin.y + 8.0)
                continue
            if ox < -0.5:
                left = max(left, -pin.x + label_len)
            elif ox > 0.5:
                right = max(right, pin.x + label_len)
            elif oy > 0.5:
                down = max(down, -pin.y + label_len)
            else:
                up = max(up, pin.y + label_len)
        up += 3.0
        down += 3.0
        return left, right, up, down

    @staticmethod
    def _outward(pin) -> tuple[float, float]:
        a = math.radians(pin.angle)
        # pin extends toward the body at `angle` (symbol coords, y-up);
        # outward in sheet coords (y-down) is the negated, y-flipped vector
        return (-round(math.cos(a), 6), round(math.sin(a), 6))

    # -- emission helpers --------------------------------------------------

    def _wire(self, a: tuple[float, float], b: tuple[float, float], key: str):
        self.body.append(
            f'\t(wire (pts (xy {a[0]:g} {a[1]:g}) (xy {b[0]:g} {b[1]:g}))\n'
            f"\t\t(stroke (width 0) (type default))\n"
            f'\t\t(uuid "{_uid(self.file_name, "wire", key)}")\n'
            f"\t)"
        )

    def _label(
        self, net: str, at: tuple[float, float], angle: int, key: str
    ):
        is_global = len(self.ir.nets[net].sheets) > 1
        justify = {
            0: "left",
            180: "right",
            90: "left",
            270: "right",
        }[angle]
        if is_global:
            self.body.append(
                f'\t(global_label "{_esc(net)}" (shape input)'
                f" (at {at[0]:g} {at[1]:g} {angle})\n"
                f"\t\t(effects (font (size 1.27 1.27)) (justify {justify}))\n"
                f'\t\t(uuid "{_uid(self.file_name, "glabel", key)}")\n'
                f"\t)"
            )
        else:
            self.body.append(
                f'\t(label "{_esc(net)}" (at {at[0]:g} {at[1]:g} {angle})\n'
                f"\t\t(effects (font (size 1.27 1.27)) (justify {justify}))\n"
                f'\t\t(uuid "{_uid(self.file_name, "label", key)}")\n'
                f"\t)"
            )

    def _power_symbol(self, net: str, at: tuple[float, float], key: str):
        self.used_power_nets.add(net)
        self.pwr_counter += 1
        ref = f"#PWR{self.pwr_counter:03d}"
        net_ir = self.ir.nets[net]
        lib = f"atopile:PWR_{_power_sym_name(net)}"
        value_y = at[1] + 5.6 if net_ir.is_gnd else at[1] - 3.6
        u = _uid(self.file_name, "pwr", key)
        self.body.append(
            f'\t(symbol (lib_id "{lib}") (at {at[0]:g} {at[1]:g} 0) (unit 1)\n'
            f"\t\t(exclude_from_sim no) (in_bom no) (on_board yes) (dnp no)\n"
            f'\t\t(uuid "{u}")\n'
            f'\t\t(property "Reference" "{ref}" (at {at[0]:g} {at[1]:g} 0)\n'
            f"\t\t\t(effects (font (size 1.27 1.27)) hide)\n"
            f"\t\t)\n"
            f'\t\t(property "Value" "{_esc(net)}"'
            f" (at {at[0]:g} {value_y:g} 0)\n"
            f"\t\t\t(effects (font (size 1.27 1.27)))\n"
            f"\t\t)\n"
            f'\t\t(pin "1" (uuid "{_uid(self.file_name, "pwrpin", key)}"))\n'
            f"\t\t(instances (project \"{_esc(self.project)}\"\n"
            f'\t\t\t(path "{self.instance_path}" (reference "{ref}") (unit 1))\n'
            f"\t\t))\n"
            f"\t)"
        )

    def _pin_termination(self, placed: _PlacedComponent, pin: _PlacedPin):
        if pin.net is None or pin.net not in self.ir.nets:
            # unconnected by design: mark with a no-connect cross
            self.body.append(
                f"\t(no_connect (at {pin.conn[0]:g} {pin.conn[1]:g})"
                f' (uuid "{_uid(self.file_name, "nc", placed.comp.address, pin.number)}"))'
            )
            return
        net = self.ir.nets[pin.net]
        key = f"{placed.comp.address}:{pin.number}"
        cx, cy = pin.conn
        ox, oy = pin.outward

        if net.is_power and abs(oy) > 0.5 and ((oy > 0) == net.is_gnd):
            # pin already exits vertically in the conventional direction:
            # terminate with a power flag
            end = (cx, cy + (STUB if net.is_gnd else -STUB) * 2)
            self._wire((cx, cy), end, key)
            self._power_symbol(pin.net, end, key)
            return

        # everything else (including sideways power pins, where a flag's
        # vertical run would cross neighbouring pins) gets a net label
        stub = STUB * 1.5
        end = (_snap(cx + ox * stub), _snap(cy + oy * stub))
        self._wire((cx, cy), end, key)
        if abs(ox) > 0.5:
            angle = 0 if ox > 0 else 180
        else:
            angle = 270 if oy > 0 else 90
        self._label(pin.net, end, angle, key)

    def _instance(self, comp: ComponentIR, at: tuple[float, float]):
        sym = self.ir.symbols[comp.lib_id]
        self.used_lib_ids.add(comp.lib_id)
        u = _uid(self.file_name, "sym", comp.address)
        x1, y1, x2, y2 = sym.bbox
        ref_pos = (at[0] + x1, at[1] - y2 - 2.0)
        val_pos = (at[0] + x1, at[1] + -y1 + 2.0)

        pin_lines = "".join(
            f'\t\t(pin "{p.number}"'
            f' (uuid "{_uid(self.file_name, "pin", comp.address, p.number)}"))\n'
            for p in sym.pins
        )
        self.body.append(
            f'\t(symbol (lib_id "{comp.lib_id}")'
            f" (at {at[0]:g} {at[1]:g} 0) (unit 1)\n"
            f"\t\t(exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)\n"
            f'\t\t(uuid "{u}")\n'
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
            f"\t\t(instances (project \"{_esc(self.project)}\"\n"
            f'\t\t\t(path "{self.instance_path}"'
            f' (reference "{_esc(comp.reference)}") (unit 1))\n'
            f"\t\t))\n"
            f"\t)"
        )

        placed = _PlacedComponent(comp=comp, sym=sym, at=at, pins=[])
        for pin in sym.pins:
            conn = (_snap(at[0] + pin.x), _snap(at[1] - pin.y))
            placed.pins.append(
                _PlacedPin(
                    number=pin.number,
                    conn=conn,
                    outward=self._outward(pin),
                    net=comp.pin_nets.get(pin.number),
                )
            )
        seen: set[str] = set()
        for p in placed.pins:
            # symbols can repeat a pin number (e.g. paralleled pins drawn
            # stacked); terminate each position once
            pin_key = f"{p.number}@{p.conn}"
            if pin_key in seen:
                continue
            seen.add(pin_key)
            self._pin_termination(placed, p)

    # -- rows ---------------------------------------------------------------

    def _place_row(self, comps: list[ComponentIR], usable_width: float):
        x = MARGIN
        row_height = 0.0
        row_started = False
        for comp in comps:
            left, right, up, down = self._component_extents(comp)
            w = left + right + COL_GAP
            if row_started and x + w > usable_width:
                self.cursor_y += row_height + ROW_GAP
                x = MARGIN
                row_height = 0.0
                row_started = False
            at = (_snap(x + left), _snap(self.cursor_y + up))
            self._instance(comp, at)
            x += w
            self.max_x = max(self.max_x, x)
            row_height = max(row_height, up + down)
            row_started = True
        if row_started:
            self.cursor_y += row_height + ROW_GAP

    def _place_sheet_boxes(self, usable_width: float):
        if not self.sheet.children:
            return
        x = MARGIN
        w, h = 60.0, 16.0
        for child in self.sheet.children:
            if x + w > usable_width:
                x = MARGIN
                self.cursor_y += h + ROW_GAP
            sheet_uuid = _uid("sheetel", child.path)
            at = (_snap(x), _snap(self.cursor_y))
            file_name = self.child_files[child.path]
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
                f"\t\t(instances (project \"{_esc(self.project)}\"\n"
                f'\t\t\t(path "{self.instance_path}" (page "?"))\n'
                f"\t\t))\n"
                f"\t)"
            )
            self.sheet_pages.append((sheet_uuid, child.path))
            x += w + COL_GAP
            self.max_x = max(self.max_x, x)
        self.cursor_y += h + ROW_GAP

    # -- top level -----------------------------------------------------------

    def render(self) -> str:
        usable_width = PAPERS["A3"][0] - 2 * MARGIN

        comps = self.sheet.components
        self._place_sheet_boxes(usable_width)
        for role in ("anchor", "series", "pull", "decoupling"):
            row = [c for c in comps if c.role == role]
            if row:
                self._place_row(row, usable_width)

        # paper selection based on extent
        paper = "A4"
        for name in ("A4", "A3", "A2", "A1"):
            w, h = PAPERS[name]
            if self.max_x + MARGIN <= w and self.cursor_y + MARGIN <= h:
                paper = name
                break
        else:
            paper = "A1"

        lib_symbols = "".join(
            "\t" + dump_sexp(self.ir.symbols[lib_id].node, 1).replace(
                f'(symbol "{self.ir.symbols[lib_id].identifier}"',
                f'(symbol "{lib_id}"',
                1,
            ) + "\n"
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

    # file names per sheet path
    files: dict[str, str] = {"": f"{target_name}.kicad_sch"}

    def collect(sheet: SheetIR):
        for child in sheet.children:
            files[child.path] = f"{target_name}.{child.path}.kicad_sch"
            collect(child)

    collect(ir.root)

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
