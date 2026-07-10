# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""CLI commands for working with PCB layouts."""

import logging
from pathlib import Path
from typing import Annotated

import typer

from atopile import errors
from atopile.telemetry import capture

logger = logging.getLogger(__name__)

layout_app = typer.Typer(rich_markup_mode="rich")


@layout_app.command("adopt-placement")
@capture("cli:layout_adopt_placement_start", "cli:layout_adopt_placement_end")
def adopt_placement_cmd(
    donor: Annotated[
        Path,
        typer.Option(
            "--from",
            "-f",
            help="Donor .kicad_pcb to take placement from",
        ),
    ],
    build: Annotated[
        str | None,
        typer.Option(
            "--build",
            "-b",
            help="Build target whose layout to update (default: only build)",
        ),
    ] = None,
    target_pcb: Annotated[
        Path | None,
        typer.Option(
            "--pcb",
            help="Explicit target .kicad_pcb (overrides --build)",
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Only report matches")
    ] = False,
    project_dir: Annotated[Path | None, typer.Option("--project-dir", "-p")] = None,
):
    """
    Adopt component placement from a hand-laid KiCad board into the layout of
    this project. Footprints are matched by footprint type and a net-name
    fingerprint, then moved to the donor positions.
    """
    from atopile.config import config
    from faebryk.exporters.pcb.layout.adopt_placement import adopt_placement
    from faebryk.libs.util import md_list

    if not donor.exists():
        raise errors.UserBadParameterError(f"Donor PCB `{donor}` does not exist")

    if target_pcb is None:
        config.apply_options(None, working_dir=project_dir)
        builds = list(config.project.builds.keys())
        if build is None:
            if len(builds) != 1:
                raise errors.UserBadParameterError(
                    f"Multiple build targets ({', '.join(builds)});"
                    " specify one with --build"
                )
            build = builds[0]
        with config.select_build(build):
            target_pcb = config.build.paths.layout

    if not target_pcb.exists():
        raise errors.UserBadParameterError(
            f"Layout `{target_pcb}` does not exist. Run `ato build` first."
        )

    result = adopt_placement(target_pcb, donor, dry_run=dry_run)

    print(f"Matched {len(result.matched)} footprints:")
    for target_ref, donor_ref, score in result.matched:
        marker = " (side differs!)" if target_ref in result.side_changes else ""
        print(f"  {target_ref:8s} <- {donor_ref:8s} (net match {score:.0%}){marker}")
    if result.unmatched_targets:
        print(
            "Unmatched in this design:\n"
            + md_list(sorted(result.unmatched_targets))
        )
    if result.unused_donors:
        print(
            "Donor footprints without counterpart:\n"
            + md_list(sorted(result.unused_donors))
        )
    if dry_run:
        print("(dry run: no changes written)")
    else:
        print(f"Updated {target_pcb}")


def _resolve_layout_path(
    build: str | None, pcb: Path | None, project_dir: Path | None
) -> Path:
    from atopile.config import config

    if pcb is not None:
        if not pcb.exists():
            raise errors.UserBadParameterError(f"PCB `{pcb}` does not exist")
        return pcb

    config.apply_options(None, working_dir=project_dir)
    builds = list(config.project.builds.keys())
    if build is None:
        if len(builds) != 1:
            raise errors.UserBadParameterError(
                f"Multiple build targets ({', '.join(builds)});"
                " specify one with --build"
            )
        build = builds[0]
    with config.select_build(build):
        path = config.build.paths.layout
    if not path.exists():
        raise errors.UserBadParameterError(
            f"Layout `{path}` does not exist. Run `ato build` first."
        )
    return path


@layout_app.command("fps")
def fps_cmd(
    like: Annotated[
        str | None,
        typer.Option(
            "--like",
            help="Glob matched against reference, value and footprint name",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="JSON output")] = False,
    build: Annotated[str | None, typer.Option("--build", "-b")] = None,
    pcb: Annotated[Path | None, typer.Option("--pcb")] = None,
    project_dir: Annotated[Path | None, typer.Option("--project-dir", "-p")] = None,
):
    """
    List footprints with reference, value, position, rotation and layer.
    Footprints outside the board outline bbox are flagged.
    """
    import json as json_mod
    from dataclasses import asdict

    from faebryk.exporters.pcb.placement_tools import list_footprints
    from faebryk.libs.kicad.fileformats import kicad

    path = _resolve_layout_path(build, pcb, project_dir)
    pcb_file = kicad.loads(kicad.pcb.PcbFile, path)
    entries = list_footprints(pcb_file.kicad_pcb, like=like)

    if as_json:
        print(json_mod.dumps([asdict(e) for e in entries], indent=2))
        return

    for e in entries:
        flag = "  OUTSIDE-OUTLINE" if e.outside_outline else ""
        print(
            f"{e.reference:10s} {e.value[:20]:20s} {e.name.split(':')[-1][:40]:40s}"
            f" ({e.x:8.3f},{e.y:8.3f}) r{e.r:<6.1f} {e.layer:5s}"
            f" uuid={e.uuid[:8]}{flag}"
        )


@layout_app.command("place")
def place_cmd(
    ref: Annotated[
        str | None, typer.Argument(help="Reference of the footprint to move")
    ] = None,
    uuid: Annotated[
        str | None,
        typer.Option("--uuid", help="UUID prefix (for non-unique references)"),
    ] = None,
    x: Annotated[float | None, typer.Option("--x", help="Absolute x (mm)")] = None,
    y: Annotated[float | None, typer.Option("--y", help="Absolute y (mm)")] = None,
    dx: Annotated[float, typer.Option("--dx", help="Relative x shift (mm)")] = 0,
    dy: Annotated[float, typer.Option("--dy", help="Relative y shift (mm)")] = 0,
    rot: Annotated[
        float | None, typer.Option("--rot", help="Absolute rotation (deg)")
    ] = None,
    layer: Annotated[
        str | None,
        typer.Option("--layer", help="Target side, e.g. F.Cu (flips the footprint)"),
    ] = None,
    moves_json: Annotated[
        str | None,
        typer.Option(
            "--json",
            help=(
                "Batch moves: JSON list of"
                ' {"ref"|"uuid", "x", "y", "dx", "dy", "rot", "layer"}'
                " (inline or a file path)"
            ),
        ),
    ] = None,
    build: Annotated[str | None, typer.Option("--build", "-b")] = None,
    pcb: Annotated[Path | None, typer.Option("--pcb")] = None,
    project_dir: Annotated[Path | None, typer.Option("--project-dir", "-p")] = None,
):
    """
    Move/rotate footprints (by reference or uuid prefix). Pads, texts and
    board side are handled like KiCad's own move/flip.
    """
    import json as json_mod

    from faebryk.exporters.pcb.placement_tools import (
        PlacementError,
        find_one_footprint,
        move_footprint,
    )
    from faebryk.libs.kicad.fileformats import kicad

    moves: list[dict] = []
    if moves_json is not None:
        candidate = Path(moves_json)
        if candidate.exists():
            moves_json = candidate.read_text()
        moves = json_mod.loads(moves_json)
        if not isinstance(moves, list):
            raise errors.UserBadParameterError("--json must be a JSON list")
    if ref is not None or uuid is not None:
        moves.append(
            {"ref": ref, "uuid": uuid, "x": x, "y": y, "dx": dx, "dy": dy,
             "rot": rot, "layer": layer}
        )
    if not moves:
        raise errors.UserBadParameterError("Nothing to move: pass REF or --json")

    path = _resolve_layout_path(build, pcb, project_dir)
    pcb_file = kicad.loads(kicad.pcb.PcbFile, path)

    try:
        for move in moves:
            fp = find_one_footprint(
                pcb_file.kicad_pcb,
                ref=move.get("ref"),
                uuid_prefix=move.get("uuid"),
            )
            desc = move_footprint(
                fp,
                x=move.get("x"),
                y=move.get("y"),
                dx=move.get("dx") or 0,
                dy=move.get("dy") or 0,
                r=move.get("rot"),
                layer=move.get("layer"),
            )
            print(f"{move.get('ref') or move.get('uuid')}: {desc}")
    except PlacementError as e:
        raise errors.UserException(str(e)) from e

    kicad.dumps(pcb_file, path)
    print(f"Updated {path}")


@layout_app.command("remove")
def remove_cmd(
    refs: Annotated[
        list[str] | None, typer.Argument(help="References of footprints to remove")
    ] = None,
    uuids: Annotated[
        list[str] | None,
        typer.Option("--uuid", help="UUID prefix (for non-unique references)"),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    build: Annotated[str | None, typer.Option("--build", "-b")] = None,
    pcb: Annotated[Path | None, typer.Option("--pcb")] = None,
    project_dir: Annotated[Path | None, typer.Option("--project-dir", "-p")] = None,
):
    """
    Remove footprints from the board (by reference or uuid prefix).
    Connected tracks are left in place; run DRC afterwards.
    """
    from faebryk.exporters.pcb.placement_tools import (
        PlacementError,
        find_one_footprint,
        remove_footprint,
    )
    from faebryk.libs.kicad.fileformats import kicad

    if not refs and not uuids:
        raise errors.UserBadParameterError("Nothing to remove")

    path = _resolve_layout_path(build, pcb, project_dir)
    pcb_file = kicad.loads(kicad.pcb.PcbFile, path)

    try:
        targets = [
            find_one_footprint(pcb_file.kicad_pcb, ref=r) for r in (refs or [])
        ] + [
            find_one_footprint(pcb_file.kicad_pcb, uuid_prefix=u)
            for u in (uuids or [])
        ]
    except PlacementError as e:
        raise errors.UserException(str(e)) from e

    for fp in targets:
        ref = next((p.value for p in fp.propertys if p.name == "Reference"), "?")
        print(
            f"remove {ref} ({fp.name}) at ({fp.at.x:g},{fp.at.y:g}) {fp.layer}"
        )
        if not dry_run:
            remove_footprint(pcb_file.kicad_pcb, fp)

    if dry_run:
        print("(dry run: no changes written)")
    else:
        kicad.dumps(pcb_file, path)
        print(f"Updated {path}")


@layout_app.command("symmetry")
def symmetry_cmd(
    axis: Annotated[
        float | None,
        typer.Option("--axis", help="Mirror axis x (default: board bbox center)"),
    ] = None,
    include: Annotated[
        str | None,
        typer.Option(
            "--include",
            help="Comma-separated globs (reference/value/footprint name)",
        ),
    ] = None,
    pair_tol: Annotated[
        float,
        typer.Option("--pair-tol", help="Max deviation (mm) to consider a pair"),
    ] = 2.0,
    fix: Annotated[
        bool, typer.Option("--fix", help="Snap pairs to perfect symmetry")
    ] = False,
    keep: Annotated[
        str, typer.Option("--keep", help="Reference side for --fix: left|right")
    ] = "left",
    build: Annotated[str | None, typer.Option("--build", "-b")] = None,
    pcb: Annotated[Path | None, typer.Option("--pcb")] = None,
    project_dir: Annotated[Path | None, typer.Option("--project-dir", "-p")] = None,
):
    """
    Check (and optionally fix) left-right mirror symmetry of footprint
    placement about a vertical axis derived from the board outline.
    """
    from faebryk.exporters.pcb.placement_tools import (
        PlacementError,
        apply_symmetry_fix,
        symmetry_report,
    )
    from faebryk.libs.kicad.fileformats import kicad

    path = _resolve_layout_path(build, pcb, project_dir)
    pcb_file = kicad.loads(kicad.pcb.PcbFile, path)

    try:
        report = symmetry_report(
            pcb_file.kicad_pcb, axis=axis, include=include, pair_tol=pair_tol
        )
    except PlacementError as e:
        raise errors.UserException(str(e)) from e

    print(f"axis: x={report.axis:g} ({report.axis_source})")
    if report.edge_deviation is not None:
        print(
            f"board outline: max mirror deviation {report.edge_deviation:.4f}mm,"
            f" {report.edge_unmatched} primitives without mirror partner"
        )
    for p in report.pairs:
        ok = "OK " if p.deviation < 1e-4 else "OFF"
        print(
            f"{ok} {p.left.reference:8s} ({p.left.x:8.3f},{p.left.y:8.3f})"
            f" r{p.left.r:<6.1f} <-> {p.right.reference:8s}"
            f" ({p.right.x:8.3f},{p.right.y:8.3f}) r{p.right.r:<6.1f}"
            f" dev=({p.dx:+.3f},{p.dy:+.3f}) rot:{p.rot_relation}"
        )
    for e, offset in report.centered:
        ok = "OK " if abs(offset) < 1e-4 else "OFF"
        print(
            f"{ok} {e.reference:8s} ({e.x:8.3f},{e.y:8.3f}) r{e.r:<6.1f}"
            f" centered, axis offset {offset:+.3f}"
        )
    for e in report.unpaired:
        print(f"--  {e.reference:8s} ({e.x:8.3f},{e.y:8.3f}) {e.layer} unpaired")

    if fix:
        try:
            changes = apply_symmetry_fix(pcb_file.kicad_pcb, report, keep=keep)
        except PlacementError as e:
            raise errors.UserException(str(e)) from e
        for c in changes:
            print(f"fix: {c}")
        if changes:
            kicad.dumps(pcb_file, path)
            print(f"Updated {path}")
        else:
            print("Already symmetric; nothing to fix")


@layout_app.command("ratsnest")
def ratsnest_cmd(
    build: Annotated[str | None, typer.Option("--build", "-b")] = None,
    pcb: Annotated[Path | None, typer.Option("--pcb")] = None,
    net: Annotated[
        str | None, typer.Option("--net", help="Only show this net")
    ] = None,
    unrouted_only: Annotated[
        bool,
        typer.Option("--unrouted", help="Only nets without copper yet"),
    ] = False,
    project_dir: Annotated[Path | None, typer.Option("--project-dir", "-p")] = None,
):
    """
    Print per-net connection points (pad coordinates) and existing copper —
    the input an agent needs to plan routing.
    """
    from faebryk.exporters.pcb.routing_tools import ratsnest
    from faebryk.libs.kicad.fileformats import kicad

    path = _resolve_layout_path(build, pcb, project_dir)
    pcb_file = kicad.loads(kicad.pcb.PcbFile, path)

    for entry in ratsnest(pcb_file.kicad_pcb):
        if net is not None and entry.net != net:
            continue
        if unrouted_only and (entry.segments or entry.zones):
            continue
        if len(entry.pads) < 2 and net is None:
            continue
        pads = " ".join(f"{p}@({x},{y},{lay})" for p, x, y, lay in entry.pads)
        print(
            f"{entry.net}: {len(entry.pads)} pads,"
            f" {entry.segments} segs, {entry.vias} vias, {entry.zones} zones\n"
            f"  {pads}"
        )


@layout_app.command("add-zone")
def add_zone_cmd(
    net: Annotated[str, typer.Option("--net", help="Net name")],
    layer: Annotated[str, typer.Option("--layer", help="Copper layer")],
    name: Annotated[str | None, typer.Option("--name", help="Zone name")] = None,
    clearance: Annotated[float, typer.Option("--clearance")] = 0.2,
    min_thickness: Annotated[float, typer.Option("--min-thickness")] = 0.2,
    priority: Annotated[int | None, typer.Option("--priority")] = None,
    build: Annotated[str | None, typer.Option("--build", "-b")] = None,
    pcb: Annotated[Path | None, typer.Option("--pcb")] = None,
    project_dir: Annotated[Path | None, typer.Option("--project-dir", "-p")] = None,
):
    """
    Add a filled zone covering the board outline (power/ground plane).
    Refill and validate with `kicad-cli pcb drc --refill-zones`.
    """
    from faebryk.exporters.pcb.routing_tools import RoutingError, add_zone
    from faebryk.libs.kicad.fileformats import kicad

    path = _resolve_layout_path(build, pcb, project_dir)
    pcb_file = kicad.loads(kicad.pcb.PcbFile, path)
    try:
        add_zone(
            pcb_file.kicad_pcb,
            net=net,
            layer=layer,
            name=name,
            clearance=clearance,
            min_thickness=min_thickness,
            priority=priority,
        )
    except RoutingError as e:
        raise errors.UserException(str(e)) from e
    kicad.dumps(pcb_file, path)
    print(f"Added zone {name or net} on {layer} to {path}")


@layout_app.command("fanout")
def fanout_cmd(
    net: Annotated[str, typer.Option("--net", help="Net to fan out")],
    via_size: Annotated[float, typer.Option("--via-size")] = 0.47,
    drill: Annotated[float, typer.Option("--drill")] = 0.25,
    track_width: Annotated[float, typer.Option("--track-width")] = 0.3,
    share_radius: Annotated[
        float,
        typer.Option(
            "--share-radius",
            help="Pads with a same-net via within this radius share it",
        ),
    ] = 1.5,
    build: Annotated[str | None, typer.Option("--build", "-b")] = None,
    pcb: Annotated[Path | None, typer.Option("--pcb")] = None,
    project_dir: Annotated[Path | None, typer.Option("--project-dir", "-p")] = None,
):
    """
    Plane fanout: drop a stitching via (plus short track) next to every
    surface pad of a net so it connects to inner-plane zones.
    """
    from faebryk.exporters.pcb.routing_tools import RoutingError, fanout_net
    from faebryk.libs.kicad.fileformats import kicad

    path = _resolve_layout_path(build, pcb, project_dir)
    pcb_file = kicad.loads(kicad.pcb.PcbFile, path)
    try:
        counts = fanout_net(
            pcb_file.kicad_pcb,
            net,
            via_size=via_size,
            drill=drill,
            track_width=track_width,
            share_radius=share_radius,
        )
    except RoutingError as e:
        raise errors.UserException(str(e)) from e
    kicad.dumps(pcb_file, path)
    failed = counts.pop("failed")
    print(f"Fanout {net}: {counts}")
    if failed:
        print(f"  could not place vias for: {', '.join(failed)}")


@layout_app.command("apply-routes")
def apply_routes_cmd(
    plan: Annotated[
        Path, typer.Argument(help="JSON route plan (tracks/vias/zones)")
    ],
    clear_generated: Annotated[
        bool,
        typer.Option(
            "--clear-generated",
            help="Remove previously generated routing first",
        ),
    ] = False,
    build: Annotated[str | None, typer.Option("--build", "-b")] = None,
    pcb: Annotated[Path | None, typer.Option("--pcb")] = None,
    project_dir: Annotated[Path | None, typer.Option("--project-dir", "-p")] = None,
):
    """
    Apply a JSON route plan (tracks, vias, zones) to the layout. See
    `faebryk.exporters.pcb.routing_tools.apply_route_plan` for the schema.
    """
    import json

    from faebryk.exporters.pcb.routing_tools import (
        RoutingError,
        apply_route_plan,
        clear_generated_routing,
    )
    from faebryk.libs.kicad.fileformats import kicad

    path = _resolve_layout_path(build, pcb, project_dir)
    pcb_file = kicad.loads(kicad.pcb.PcbFile, path)

    if clear_generated:
        removed = clear_generated_routing(pcb_file.kicad_pcb)
        print(f"Removed generated routing: {removed}")

    try:
        plan_data = json.loads(plan.read_text())
        counts = apply_route_plan(pcb_file.kicad_pcb, plan_data)
    except RoutingError as e:
        raise errors.UserException(str(e)) from e

    kicad.dumps(pcb_file, path)
    print(f"Applied route plan to {path}: {counts}")
