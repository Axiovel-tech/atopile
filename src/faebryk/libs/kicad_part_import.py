# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""
Import parts (atomic parts) from local KiCad libraries.

This enables `ato create part-from-kicad`, which builds a part directory
(<parts>/<Manufacturer>_<Partnumber>/) from:
 - a symbol out of a `.kicad_sym` symbol library (any KiCad version), and
 - a `.kicad_mod` footprint (modern or legacy `(module ...)` format).

Unlike the EasyEDA ingestion path, real-world KiCad symbol libraries
(`kicad_symbol_lib`) are not parseable by the strict typed sexp models, so the
symbol is handled with a small tolerant generic sexp parser and copied
verbatim into the part. Footprints are normalized through the typed models
(with a v5 fallback) because the PCB build pipeline must be able to load them.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from faebryk.libs.codegen.atocodegen import AtoCodeGen
from faebryk.libs.codegen.pycodegen import sanitize_name
from faebryk.libs.kicad.fileformats import kicad
from faebryk.libs.kicad.sexp_tools import (
    SexpAtom,
    SexpNode,
    dump_sexp,
    parse_sexp,
    sexp_atom_arg as _atom_arg,
    sexp_children as _children,
    sexp_tag as _tag,
)
from faebryk.libs.util import sanitize_filepath_part, starts_or_ends_replace

logger = logging.getLogger(__name__)


class KicadPartImportError(Exception):
    pass


# ---------------------------------------------------------------------------
# Symbol library handling
# ---------------------------------------------------------------------------


@dataclass
class KicadLibrarySymbol:
    name: str
    node: list[SexpNode]
    lib_version: str
    extends_node: list[SexpNode] | None = None

    #: (sanitized_or_None_name, pin_number) pairs; name None == numeric pin
    pins: list[tuple[str | None, str]] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)


def load_library_symbol(
    symbol_lib_path: Path, symbol_name: str | None
) -> KicadLibrarySymbol:
    """
    Load one symbol (plus its `extends` parent, if any) from a
    `kicad_symbol_lib` or `kicad_sym` library file.
    """
    try:
        root = parse_sexp(symbol_lib_path.read_text(encoding="utf-8"))
    except OSError as ex:
        raise KicadPartImportError(
            f"Cannot read symbol library `{symbol_lib_path}`: {ex}"
        ) from ex

    root_tag = _tag(root)
    if root_tag not in ("kicad_symbol_lib", "kicad_sym"):
        raise KicadPartImportError(
            f"`{symbol_lib_path}` is not a KiCad symbol library"
            f" (root token `{root_tag}`)"
        )

    symbols = {
        name: s
        for s in _children(root, "symbol")
        if (name := _atom_arg(s)) is not None
    }
    if not symbols:
        raise KicadPartImportError(f"No symbols found in `{symbol_lib_path}`")

    if symbol_name is None:
        if len(symbols) > 1:
            raise KicadPartImportError(
                f"Symbol library `{symbol_lib_path}` contains multiple symbols."
                f" Specify one of: {', '.join(sorted(symbols))}"
            )
        symbol_name = next(iter(symbols))

    if symbol_name not in symbols:
        raise KicadPartImportError(
            f"Symbol `{symbol_name}` not found in `{symbol_lib_path}`."
            f" Available: {', '.join(sorted(symbols))}"
        )

    node = symbols[symbol_name]

    version = "20241229"
    if version_nodes := _children(root, "version"):
        version = _atom_arg(version_nodes[0]) or version

    out = KicadLibrarySymbol(name=symbol_name, node=node, lib_version=version)

    # resolve `extends` (derived symbol): pins/graphics live on the parent
    pin_source = node
    if extends := _children(node, "extends"):
        parent_name = _atom_arg(extends[0])
        if parent_name not in symbols:
            raise KicadPartImportError(
                f"Symbol `{symbol_name}` extends `{parent_name}`,"
                f" which is missing from `{symbol_lib_path}`"
            )
        out.extends_node = symbols[parent_name]
        pin_source = symbols[parent_name]

    # properties
    for prop in _children(node, "property"):
        args = [c for c in prop[1:] if isinstance(c, SexpAtom)]
        if len(args) >= 2:
            out.properties[args[0].value] = args[1].value

    # pins live either nested in unit symbols (symbol "NAME_x_y" ... (pin ...))
    # or directly on the symbol (flat, SamacSys-style libraries)
    pin_nodes = list(_children(pin_source, "pin"))
    for unit in _children(pin_source, "symbol"):
        pin_nodes.extend(_children(unit, "pin"))

    for pin in pin_nodes:
        name = None
        number = None
        if name_nodes := _children(pin, "name"):
            name = _atom_arg(name_nodes[0])
        if number_nodes := _children(pin, "number"):
            number = _atom_arg(number_nodes[0])
        if number is None:
            continue
        out.pins.append((name, number))

    if not out.pins:
        raise KicadPartImportError(
            f"Symbol `{symbol_name}` in `{symbol_lib_path}` has no pins"
        )

    return out


def dump_symbol_lib(symbol: KicadLibrarySymbol) -> str:
    """Emit a single-part symbol library containing the symbol (and parent)."""
    parts: list[str] = [
        "(kicad_symbol_lib",
        f"\t(version {symbol.lib_version})",
        '\t(generator "atopile_kicad_import")',
    ]
    if symbol.extends_node is not None:
        parts.append("\t" + dump_sexp(symbol.extends_node, 1))
    parts.append("\t" + dump_sexp(symbol.node, 1))
    parts.append(")")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Footprint handling
# ---------------------------------------------------------------------------


def load_footprint(footprint_path: Path) -> kicad.footprint.FootprintFile:
    """
    Load a `.kicad_mod` file, accepting both the modern `(footprint ...)`
    format and the legacy `(module ...)` format (converted on the fly).
    """
    if not footprint_path.exists():
        raise KicadPartImportError(f"Footprint `{footprint_path}` does not exist")

    try:
        return kicad.loads(kicad.footprint.FootprintFile, footprint_path)
    except Exception as modern_ex:
        try:
            fp_v5 = kicad.loads(kicad.footprint_v5.FootprintFile, footprint_path)
            return kicad.convert(fp_v5)
        except Exception:
            raise KicadPartImportError(
                f"Cannot parse footprint `{footprint_path}`: {modern_ex}"
            ) from modern_ex


# ---------------------------------------------------------------------------
# Part generation
# ---------------------------------------------------------------------------


def _sanitize_pin_name(pin_name: str, identifier: str) -> str | None:
    # mirrors AtoPart._dump_pins sanitization
    if re.match(r"^[0-9]+$", pin_name):
        return None
    pin_name = starts_or_ends_replace(pin_name, ("~", "#"), prefix="n")
    pin_name = starts_or_ends_replace(pin_name, ("+",), suffix="pos")
    pin_name = starts_or_ends_replace(pin_name, ("-", "–"), suffix="neg")
    return sanitize_name(pin_name, warn_prefix=f"{identifier}")


def _dump_pins(
    build: AtoCodeGen.ComponentFile,
    pins: list[tuple[str | None, str]],
    identifier: str,
):
    from natsort import natsorted

    build.add_comments("pins", use_spacer=True)

    unsorted_pins: list[tuple[str | None, str]] = []
    for pin_name, pin_num in pins:
        if pin_name is not None and pin_name.upper() == "NC":
            continue
        # "" and "~" mean "unnamed" in KiCad symbol libraries
        if pin_name in (None, "", "~"):
            sanitized = None
        else:
            assert pin_name is not None
            sanitized = _sanitize_pin_name(pin_name, identifier)
        unsorted_pins.append((sanitized, pin_num))

    sorted_pins = natsorted(unsorted_pins, key=lambda x: (x[0] is None, x[0], x[1]))
    all_pin_nums = {pin_num for _, pin_num in sorted_pins}
    defined_signals: set[str] = set()
    emitted: set[tuple[str | None, str]] = set()

    for pin_name, pin_num in sorted_pins:
        if (pin_name, pin_num) in emitted:
            continue
        emitted.add((pin_name, pin_num))
        if pin_name is None:
            build.add_stmt(AtoCodeGen.PinDeclaration(pin_num))
        else:
            signal_name = f"SIG_{pin_name}" if pin_name in all_pin_nums else pin_name
            build.add_stmt(
                AtoCodeGen.Connect(
                    left=AtoCodeGen.Connect.Connectable(
                        signal_name,
                        declare="signal"
                        if signal_name not in defined_signals
                        else None,
                    ),
                    right=AtoCodeGen.Connect.Connectable(pin_num, declare="pin"),
                )
            )
            defined_signals.add(signal_name)


@dataclass(kw_only=True)
class KicadImportedPart:
    identifier: str
    path: Path
    module_name: str

    def generate_import_statement(self, src_path: Path) -> str:
        ato_path = self.path / (self.path.name + ".ato")
        import_path = ato_path.relative_to(src_path)
        return f'from "{import_path}" import {self.module_name}'


def synthesize_symbol_lib_text(
    symbol_name: str,
    pins: list[tuple[str, str]],
    *,
    reference_prefix: str = "U",
) -> str:
    """
    Generate a minimal KiCad symbol library containing a box symbol with the
    given pins ((number, name) pairs). Pins are split evenly between the left
    and right side of the box, on a 2.54 mm grid.
    """
    n = len(pins)
    left = pins[: (n + 1) // 2]
    right = pins[(n + 1) // 2 :]
    rows = max(len(left), len(right))

    height = (rows + 1) * 2.54
    width = 20.32
    top = height / 2
    half_w = width / 2

    def fmt(v: float) -> str:
        return f"{round(v, 2):g}"

    pin_lines: list[str] = []
    for i, (number, name) in enumerate(left):
        y = top - (i + 1) * 2.54
        pin_lines.append(
            f'\t\t\t(pin passive line (at {fmt(-half_w - 2.54)} {fmt(y)} 0)'
            f' (length 2.54)\n'
            f'\t\t\t\t(name "{name}" (effects (font (size 1.27 1.27))))\n'
            f'\t\t\t\t(number "{number}" (effects (font (size 1.27 1.27))))\n'
            f"\t\t\t)"
        )
    for i, (number, name) in enumerate(right):
        y = top - (i + 1) * 2.54
        pin_lines.append(
            f'\t\t\t(pin passive line (at {fmt(half_w + 2.54)} {fmt(y)} 180)'
            f' (length 2.54)\n'
            f'\t\t\t\t(name "{name}" (effects (font (size 1.27 1.27))))\n'
            f'\t\t\t\t(number "{number}" (effects (font (size 1.27 1.27))))\n'
            f"\t\t\t)"
        )

    pins_block = "\n".join(pin_lines)
    return f'''(kicad_symbol_lib
\t(version 20241229)
\t(generator "atopile_pinout_import")
\t(symbol "{symbol_name}"
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(property "Reference" "{reference_prefix}" (at 0 {fmt(top + 1.27)} 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "{symbol_name}" (at 0 {fmt(-top - 1.27)} 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(symbol "{symbol_name}_1_1"
\t\t\t(rectangle (start {fmt(-half_w)} {fmt(top)}) (end {fmt(half_w)} {fmt(-top)})
\t\t\t\t(stroke (width 0.254) (type default))
\t\t\t\t(fill (type background))
\t\t\t)
{pins_block}
\t\t)
\t)
)
'''


def import_part_from_pinout(
    *,
    footprint_path: Path,
    pins: list[tuple[str, str]],
    manufacturer: str,
    partnumber: str,
    datasheet: str | None = None,
    supplier_partno: str | None = None,
    designator_prefix: str = "U",
    docstring: str = "",
    overwrite: bool = False,
) -> KicadImportedPart:
    """
    Create an atomic part from a footprint plus an explicit pin map,
    synthesizing the schematic symbol. The offline path for parts that exist
    in no local KiCad library.
    """
    import tempfile

    symbol_name = sanitize_filepath_part(partnumber)
    text = synthesize_symbol_lib_text(
        symbol_name, pins, reference_prefix=designator_prefix
    )
    with tempfile.NamedTemporaryFile(
        "w", suffix=".kicad_sym", delete=False
    ) as f:
        f.write(text)
        tmp_path = Path(f.name)
    try:
        return import_part_from_kicad(
            symbol_lib_path=tmp_path,
            footprint_path=footprint_path,
            symbol_name=symbol_name,
            manufacturer=manufacturer,
            partnumber=partnumber,
            datasheet=datasheet,
            supplier_partno=supplier_partno,
            docstring=docstring,
            overwrite=overwrite,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def import_part_from_kicad(
    *,
    symbol_lib_path: Path,
    footprint_path: Path,
    symbol_name: str | None = None,
    manufacturer: str | None = None,
    partnumber: str | None = None,
    datasheet: str | None = None,
    supplier_partno: str | None = None,
    docstring: str = "",
    overwrite: bool = False,
) -> KicadImportedPart:
    """
    Create an atomic part in the project's parts directory from local KiCad
    library files. Returns the created part info.
    """
    from atopile.config import config as Gcfg
    from faebryk.libs.part_lifecycle import PartLifecycle

    symbol = load_library_symbol(symbol_lib_path, symbol_name)

    # defaults pulled from symbol properties where sensible
    if manufacturer is None:
        manufacturer = symbol.properties.get("Manufacturer") or "UNKNOWN"
    if partnumber is None:
        partnumber = (
            symbol.properties.get("MPN")
            or symbol.properties.get("Part Number")
            or symbol.name
        )
    if datasheet is None:
        datasheet = symbol.properties.get("Datasheet") or None
        if datasheet in ("", "~"):
            datasheet = None
    if supplier_partno is None:
        supplier_partno = (
            symbol.properties.get("LCSC")
            or symbol.properties.get("LCSC Part")
            or f"MANUAL-{partnumber}"
        )

    identifier = "_".join(
        sanitize_filepath_part(x) for x in (manufacturer, partnumber)
    )
    module_name = f"{identifier}_package"

    parts_dir = Gcfg.project.paths.parts
    part_dir = parts_dir / identifier
    if part_dir.exists() and not overwrite:
        raise KicadPartImportError(
            f"Part directory `{part_dir}` already exists."
            " Use --overwrite to replace it."
        )

    fp = load_footprint(footprint_path)
    # normalize: identifier-based library name, like AtoPart does
    fp = kicad.copy(fp)
    fp_base_name = kicad.fp_get_base_name(fp.footprint)
    fp.footprint.name = f"{identifier}:{fp_base_name}"

    designator_prefix = symbol.properties.get("Reference", "U")

    # build the .ato file
    cf = AtoCodeGen.ComponentFile(module_name, docstring=docstring or None)
    cf.add_comments(
        f"Imported from KiCad library `{symbol_lib_path.name}`"
        f" (symbol `{symbol.name}`)",
    )
    cf.add_trait(
        "is_atomic_part",
        manufacturer=manufacturer,
        partnumber=partnumber,
        footprint=f"{fp_base_name}.kicad_mod",
        symbol=f"{symbol.name}.kicad_sym",
    )
    cf.add_trait(
        "has_part_picked",
        "by_supplier",
        supplier_id="lcsc",
        supplier_partno=supplier_partno,
        manufacturer=manufacturer,
        partno=partnumber,
    )
    cf.add_trait("has_designator_prefix", prefix=designator_prefix)
    if datasheet:
        cf.add_trait("has_datasheet", datasheet=datasheet)

    _dump_pins(cf, symbol.pins, identifier)

    ato_content = cf.dump()

    # write everything
    part_dir.mkdir(parents=True, exist_ok=True)
    kicad.dumps(fp, part_dir / f"{fp_base_name}.kicad_mod")
    (part_dir / f"{symbol.name}.kicad_sym").write_text(
        dump_symbol_lib(symbol), encoding="utf-8"
    )
    (part_dir / f"{identifier}.ato").write_text(ato_content, encoding="utf-8")

    # register footprint library for all build targets
    PartLifecycle.singleton().library._insert_fp_lib(identifier)

    logger.info(f"Created part `{identifier}` at `{part_dir}`")

    return KicadImportedPart(
        identifier=identifier, path=part_dir, module_name=module_name
    )
