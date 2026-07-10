# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""
Schematic IR extraction (v1 provider).

Builds the schematic intermediate representation from the *built artifacts*:
the `.kicad_pcb` (authoritative netlist: designators, pad->net, and the
`atopile_address` module hierarchy) plus the project parts directory (the
schematic symbol of every atomic part).

This keeps the exporter decoupled from the instance graph; a graph-based
provider with richer semantics (interface types, bridge chains) can later
produce the same IR.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from faebryk.libs.kicad.fileformats import kicad
from faebryk.libs.kicad.sexp_tools import (
    SexpAtom,
    SexpNode,
    parse_sexp,
    sexp_children,
    sexp_tag,
)

logger = logging.getLogger(__name__)

#: a module with at least this many components in its subtree gets a sheet;
#: smaller modules are inlined into the parent sheet as visual clusters
SHEET_MIN_COMPONENTS = 8

_POWER_NET_RE = re.compile(r"^(\+|GND|VBAT|VBUS$|GVDD|VCC|VDD|VEE|VSS)")
_GND_NET_RE = re.compile(r"^(GND|VEE|VSS)")


class SchematicExportError(Exception):
    pass


@dataclass
class SymbolPin:
    number: str
    name: str
    x: float
    y: float
    angle: float
    length: float


@dataclass
class SymbolDef:
    """A part's schematic symbol: raw sexp body + extracted geometry."""

    identifier: str  # part identifier == lib symbol name
    node: SexpNode  # raw (symbol ...) subtree, renamed
    pins: list[SymbolPin]
    # body bounding box in symbol coords (y-up), including pins
    bbox: tuple[float, float, float, float]


@dataclass
class ComponentIR:
    address: str
    reference: str
    value: str
    lib_id: str  # "atopile:<part identifier>"
    sheet: str  # sheet path ("" == root)
    #: pad/pin number -> net name
    pin_nets: dict[str, str] = field(default_factory=dict)
    #: anchor (IC/connector), series, pull, decoupling
    role: str = "anchor"
    #: original parent module path (cluster key when modules are inlined)
    cluster: str = ""


@dataclass
class NetIR:
    name: str
    pad_count: int = 0
    sheets: set[str] = field(default_factory=set)
    is_power: bool = False
    is_gnd: bool = False


@dataclass
class SheetIR:
    path: str  # "" for root, else dotted module path
    name: str
    components: list[ComponentIR] = field(default_factory=list)
    children: list["SheetIR"] = field(default_factory=list)


@dataclass
class SchematicIR:
    root: SheetIR
    nets: dict[str, NetIR]
    symbols: dict[str, SymbolDef]  # lib_id -> def


# ---------------------------------------------------------------------------
# symbol loading
# ---------------------------------------------------------------------------


def _walk_pins(symbol_node: SexpNode) -> list[SymbolPin]:
    pins: list[SymbolPin] = []

    def collect(node: SexpNode) -> None:
        for pin in sexp_children(node, "pin"):
            at = sexp_children(pin, "at")
            number = sexp_children(pin, "number")
            name = sexp_children(pin, "name")
            length = sexp_children(pin, "length")
            if not at or not number:
                continue
            at_args = [a for a in at[0][1:] if isinstance(a, SexpAtom)]
            pins.append(
                SymbolPin(
                    number=next(
                        (a.value for a in number[0][1:] if isinstance(a, SexpAtom)),
                        "?",
                    ),
                    name=next(
                        (a.value for a in name[0][1:] if isinstance(a, SexpAtom)),
                        "",
                    )
                    if name
                    else "",
                    x=float(at_args[0].value),
                    y=float(at_args[1].value),
                    angle=float(at_args[2].value) if len(at_args) > 2 else 0.0,
                    length=float(
                        next(
                            (
                                a.value
                                for a in length[0][1:]
                                if isinstance(a, SexpAtom)
                            ),
                            "2.54",
                        )
                    )
                    if length
                    else 2.54,
                )
            )
        for unit in sexp_children(node, "symbol"):
            collect(unit)

    collect(symbol_node)
    return pins


def _symbol_bbox(
    symbol_node: SexpNode, pins: list[SymbolPin]
) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []

    def collect_geo(node: SexpNode) -> None:
        for tag in ("rectangle", "polyline", "circle", "arc"):
            for el in sexp_children(node, tag):
                for ptag in ("start", "end", "center", "mid"):
                    for c in sexp_children(el, ptag):
                        args = [a for a in c[1:] if isinstance(a, SexpAtom)]
                        if len(args) >= 2:
                            xs.append(float(args[0].value))
                            ys.append(float(args[1].value))
                for pts in sexp_children(el, "pts"):
                    for xy in sexp_children(pts, "xy"):
                        args = [a for a in xy[1:] if isinstance(a, SexpAtom)]
                        if len(args) >= 2:
                            xs.append(float(args[0].value))
                            ys.append(float(args[1].value))
        for unit in sexp_children(node, "symbol"):
            collect_geo(unit)

    collect_geo(symbol_node)
    for pin in pins:
        xs.append(pin.x)
        ys.append(pin.y)
    if not xs:
        xs, ys = [-2.54, 2.54], [-2.54, 2.54]
    return min(xs), min(ys), max(xs), max(ys)


def _rename_symbol(node: list[SexpNode], new_name: str) -> None:
    """Rename a lib symbol (and its inner unit prefixes) in place."""
    old_name = None
    for child in node[1:]:
        if isinstance(child, SexpAtom):
            old_name = child.value
            node[node.index(child)] = SexpAtom.string(new_name)
            break
    if old_name is None:
        return
    for unit in sexp_children(node, "symbol"):
        for child in unit[1:]:
            if isinstance(child, SexpAtom):
                inner = child.value
                if inner.startswith(old_name + "_"):
                    unit[unit.index(child)] = SexpAtom.string(
                        new_name + inner[len(old_name) :]
                    )
                break


def _normalize_symbol_graphics(node: SexpNode) -> None:
    """
    Normalize legacy graphic constructs to the modern symbol grammar:
    circles defined by (center)+(end) become (center)+(radius), which is the
    only form the KiCad schematic parser accepts inside symbols.
    """
    if not isinstance(node, list):
        return
    if sexp_tag(node) == "circle":
        centers = sexp_children(node, "center")
        ends = sexp_children(node, "end")
        if centers and ends and not sexp_children(node, "radius"):
            c_args = [a for a in centers[0][1:] if isinstance(a, SexpAtom)]
            e_args = [a for a in ends[0][1:] if isinstance(a, SexpAtom)]
            cx, cy = float(c_args[0].value), float(c_args[1].value)
            ex, ey = float(e_args[0].value), float(e_args[1].value)
            radius = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
            node[node.index(ends[0])] = [
                SexpAtom.symbol("radius"),
                SexpAtom.symbol(f"{radius:.4g}"),
            ]
    for child in node:
        _normalize_symbol_graphics(child)


def load_part_symbol(part_dir: Path, identifier: str) -> SymbolDef:
    sym_files = sorted(part_dir.glob("*.kicad_sym"))
    if not sym_files:
        raise SchematicExportError(
            f"Part `{part_dir.name}` has no .kicad_sym symbol"
        )
    root = parse_sexp(sym_files[0].read_text(encoding="utf-8"))
    symbols = [
        s
        for s in sexp_children(root, "symbol")
        # skip derived symbols; the parent carries the geometry
        if not sexp_children(s, "extends")
    ]
    if not symbols:
        raise SchematicExportError(
            f"No symbol found in `{sym_files[0]}`"
        )
    node = symbols[0]
    _rename_symbol(node, identifier)
    _normalize_symbol_graphics(node)
    pins = _walk_pins(node)
    bbox = _symbol_bbox(node, pins)
    return SymbolDef(identifier=identifier, node=node, pins=pins, bbox=bbox)


# ---------------------------------------------------------------------------
# IR construction
# ---------------------------------------------------------------------------


def _classify_role(comp: ComponentIR, nets: dict[str, NetIR]) -> str:
    connected = [n for n in comp.pin_nets.values() if n in nets]
    if len(comp.pin_nets) > 2:
        return "anchor"
    kinds = [nets[n].is_power for n in connected]
    if len(kinds) == 2:
        if all(kinds):
            return "decoupling"
        if any(kinds):
            return "pull"
    return "series"


def build_ir(pcb_path: Path, parts_dir: Path, *, root_name: str) -> SchematicIR:
    pcb_file = kicad.loads(kicad.pcb.PcbFile, pcb_path)
    pcb = pcb_file.kicad_pcb

    components: list[ComponentIR] = []
    nets: dict[str, NetIR] = {}
    symbols: dict[str, SymbolDef] = {}

    for fp in pcb.footprints:
        props = {p.name: p.value for p in fp.propertys}
        address = props.get("atopile_address")
        if not address:
            logger.warning(
                f"Skipping footprint without atopile_address:"
                f" {props.get('Reference', fp.name)}"
            )
            continue
        part_identifier = fp.name.split(":")[0]
        lib_id = f"atopile:{part_identifier}"

        if lib_id not in symbols:
            part_dir = parts_dir / part_identifier
            if not part_dir.exists():
                raise SchematicExportError(
                    f"Part directory `{part_dir}` not found for `{address}`"
                )
            symbols[lib_id] = load_part_symbol(part_dir, part_identifier)

        comp = ComponentIR(
            address=address,
            reference=props.get("Reference", "?"),
            value=props.get("Value", ""),
            lib_id=lib_id,
            sheet="",  # assigned below
        )
        for pad in fp.pads:
            if pad.net and pad.net.name:
                comp.pin_nets[pad.name] = pad.net.name
        components.append(comp)

    # nets
    for comp in components:
        for net_name in comp.pin_nets.values():
            net = nets.setdefault(net_name, NetIR(name=net_name))
            net.pad_count += 1
    for net in nets.values():
        net.is_power = bool(_POWER_NET_RE.match(net.name))
        net.is_gnd = bool(_GND_NET_RE.match(net.name))

    # drop single-pad nets from labeling entirely (unconnected pins)
    nets = {n: net for n, net in nets.items() if net.pad_count > 1}

    # roles
    for comp in components:
        comp.role = _classify_role(comp, nets)

    # sheet assignment: deepest ancestor module with enough components
    def parent_path(address: str) -> str:
        return address.rsplit(".", 1)[0] if "." in address else ""

    subtree_counts: dict[str, int] = {}
    for comp in components:
        path = parent_path(comp.address)
        while path:
            subtree_counts[path] = subtree_counts.get(path, 0) + 1
            path = parent_path(path)

    sheet_paths = {
        path
        for path, count in subtree_counts.items()
        if count >= SHEET_MIN_COMPONENTS
    }

    for comp in components:
        original_parent = parent_path(comp.address)
        path = original_parent
        while path and path not in sheet_paths:
            path = parent_path(path)
        comp.sheet = path
        # cluster = the original module relative to the sheet it landed in
        if original_parent != path:
            rel = original_parent[len(path) + 1 :] if path else original_parent
            # use only the first level below the sheet as the visual cluster
            comp.cluster = rel.split(".")[0]
        else:
            comp.cluster = ""

    # net -> sheets
    for comp in components:
        for net_name in comp.pin_nets.values():
            if net_name in nets:
                nets[net_name].sheets.add(comp.sheet)

    # sheet tree (sheets nest under their closest sheet ancestor)
    root = SheetIR(path="", name=root_name)
    sheet_irs: dict[str, SheetIR] = {"": root}
    for path in sorted(sheet_paths, key=lambda p: p.count(".")):
        sheet_irs[path] = SheetIR(path=path, name=path.rsplit(".", 1)[-1])
    for path, sheet in sheet_irs.items():
        if path == "":
            continue
        parent = parent_path(path)
        while parent and parent not in sheet_irs:
            parent = parent_path(parent)
        sheet_irs[parent].children.append(sheet)
    for comp in components:
        sheet_irs[comp.sheet].components.append(comp)

    # deterministic ordering
    for sheet in sheet_irs.values():
        sheet.components.sort(key=lambda c: (c.role != "anchor", c.reference))
        sheet.children.sort(key=lambda s: s.path)

    return SchematicIR(root=root, nets=nets, symbols=symbols)
