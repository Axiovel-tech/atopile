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
