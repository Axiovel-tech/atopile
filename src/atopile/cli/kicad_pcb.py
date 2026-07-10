"""CLI command definition for `ato kicad-pcb`.

Tools that operate directly on native `.kicad_pcb` files — no ato project
required. Useful for KiCad-native boards where the KiCad project is the
source of truth and atopile serves as headless tooling: outline authoring,
stackup import, placement round-trips, and placement checks.
"""

import json
from pathlib import Path
from typing import Annotated, Optional

import typer

from atopile.logging import get_logger

kicad_pcb_app = typer.Typer(rich_markup_mode="rich", no_args_is_help=True)

logger = get_logger(__name__)


def _load(board: Path):
    from faebryk.libs.kicad.board_ops import load_board_text

    return load_board_text(board.read_text(encoding="utf-8"))


def _save(board: Path, pcb) -> None:
    from faebryk.libs.kicad.fileformats import kicad

    board.write_text(kicad.dumps(pcb), encoding="utf-8")


@kicad_pcb_app.command()
def stats(
    board: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable")] = False,
):
    """Summarize a `.kicad_pcb`: outline size, layers, footprints, nets."""
    from faebryk.libs.kicad.board_ops import summarize

    summary = summarize(_load(board))
    if as_json:
        typer.echo(json.dumps(summary.as_dict(), indent=2))
        return
    size = (
        f"{summary.size_mm[0]} x {summary.size_mm[1]} mm"
        if summary.size_mm
        else "no outline"
    )
    typer.echo(f"board:      {board}")
    typer.echo(f"outline:    {size}")
    typer.echo(
        f"layers:     {summary.layer_count} total, "
        f"{len(summary.copper_layers)} copper "
        f"({', '.join(summary.copper_layers)})"
    )
    typer.echo(
        f"footprints: {summary.footprints} "
        f"(front {summary.footprints_front}, back {summary.footprints_back})"
    )
    typer.echo(
        f"nets:       {summary.nets} | pads {summary.pads_total} "
        f"({summary.pads_unconnected} unconnected)"
    )
    typer.echo(
        f"routing:    {summary.tracks} tracks, {summary.vias} vias, "
        f"{summary.zones} zones"
    )


@kicad_pcb_app.command()
def outline(
    board: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    width: Annotated[float, typer.Option(help="Board width in mm")],
    height: Annotated[float, typer.Option(help="Board height in mm")],
    corner_radius: Annotated[float, typer.Option(help="Corner radius in mm")] = 0.0,
    origin: Annotated[
        str, typer.Option(help="Top-left corner as 'x,y' in page coordinates")
    ] = "0,0",
    keep_existing: Annotated[
        bool, typer.Option(help="Keep existing Edge.Cuts geometry")
    ] = False,
):
    """Draw a (rounded-)rectangular board outline on Edge.Cuts."""
    from faebryk.libs.kicad.board_ops import set_rectangular_outline

    ox, oy = (float(v) for v in origin.split(","))
    pcb = _load(board)
    set_rectangular_outline(
        pcb,
        width_mm=width,
        height_mm=height,
        corner_radius_mm=corner_radius,
        origin=(ox, oy),
        replace=not keep_existing,
    )
    _save(board, pcb)
    typer.echo(f"outline: {width} x {height} mm (r={corner_radius}) at ({ox}, {oy})")


@kicad_pcb_app.command()
def copy_setup(
    board: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    donor: Annotated[
        Path,
        typer.Option(
            "--from",
            "-f",
            exists=True,
            dir_okay=False,
            help="Donor .kicad_pcb (e.g. a manufacturer profile board)",
        ),
    ],
    rename: Annotated[
        Optional[list[str]],
        typer.Option("--rename", "-r", help="Rename a donor layer, format OLD=NEW"),
    ] = None,
):
    """Import the layer table and `(setup ...)` (stackup, rules) from a donor
    board — the headless twin of KiCad's Board Setup > Import Settings."""
    from faebryk.libs.kicad.board_ops import copy_setup_text

    renames = {}
    for item in rename or []:
        old, _, new = item.partition("=")
        if not new:
            raise typer.BadParameter(f"--rename expects OLD=NEW, got {item!r}")
        renames[old] = new
    out = copy_setup_text(
        board.read_text(encoding="utf-8"),
        donor.read_text(encoding="utf-8"),
        renames,
    )
    board.write_text(out, encoding="utf-8")
    typer.echo(f"setup + layers imported from {donor}")
    for old, new in renames.items():
        typer.echo(f"  layer renamed: {old} -> {new}")


@kicad_pcb_app.command()
def dump_placement(
    board: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    output: Annotated[
        Optional[Path], typer.Option("--output", "-o", help="Write TOML here")
    ] = None,
):
    """Export footprint placement (ref -> x/y/rotation/layer) as TOML."""
    from faebryk.libs.kicad.board_ops import dump_placement as _dump

    placement = _dump(_load(board))
    lines = []
    for ref in sorted(placement):
        p = placement[ref]
        lines.append(f'["{ref}"]')
        lines.append(f"x = {p.x}")
        lines.append(f"y = {p.y}")
        lines.append(f"rotation = {p.rotation}")
        lines.append(f'layer = "{p.layer}"')
        lines.append("")
    text = "\n".join(lines)
    if output:
        output.write_text(text, encoding="utf-8")
        typer.echo(f"{len(placement)} placements -> {output}")
    else:
        typer.echo(text)


@kicad_pcb_app.command()
def apply_placement(
    board: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    placement_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    lenient: Annotated[
        bool, typer.Option(help="Skip refs that are not on the board")
    ] = False,
):
    """Apply a TOML placement (as produced by dump-placement) to the board."""
    import tomllib

    from faebryk.libs.kicad.board_ops import Placement
    from faebryk.libs.kicad.board_ops import apply_placement as _apply

    raw = tomllib.loads(placement_file.read_text(encoding="utf-8"))
    placement = {
        ref: Placement(
            x=float(entry["x"]),
            y=float(entry["y"]),
            rotation=float(entry.get("rotation", 0.0)),
            layer=str(entry.get("layer", "F.Cu")),
        )
        for ref, entry in raw.items()
    }
    pcb = _load(board)
    applied = _apply(pcb, placement, strict=not lenient)
    _save(board, pcb)
    typer.echo(f"applied {len(applied)} placements to {board}")


@kicad_pcb_app.command()
def check(
    board: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    min_area: Annotated[
        float, typer.Option(help="Ignore overlaps smaller than this (mm^2)")
    ] = 0.0,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable")] = False,
):
    """Report courtyard-bbox overlaps between same-side footprints.

    A fast, dependency-free placement gate; exits non-zero on findings.
    """
    from faebryk.libs.kicad.board_ops import check_courtyard_overlaps

    overlaps = check_courtyard_overlaps(_load(board), min_area_mm2=min_area)
    if as_json:
        typer.echo(
            json.dumps(
                [
                    {
                        "ref_a": o.ref_a,
                        "ref_b": o.ref_b,
                        "area_mm2": o.area_mm2,
                        "bbox": o.bbox,
                    }
                    for o in overlaps
                ],
                indent=2,
            )
        )
    else:
        for o in overlaps:
            typer.echo(f"overlap: {o.ref_a} <-> {o.ref_b} ({o.area_mm2} mm^2)")
        typer.echo(f"{len(overlaps)} courtyard-bbox overlap(s)")
    if overlaps:
        raise typer.Exit(code=1)
