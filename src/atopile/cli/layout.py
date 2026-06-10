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
