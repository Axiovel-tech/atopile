# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

import pytest

import faebryk.library._F as F  # noqa: F401
from faebryk.exporters.pcb.placement_tools import (
    PlacementError,
    apply_symmetry_fix,
    find_one_footprint,
    list_footprints,
    move_footprint,
    remove_footprint,
    symmetry_report,
)
from faebryk.libs.kicad.fileformats import kicad
from faebryk.libs.test.fileformats import PCBFILE

AXIS = 50.0


@pytest.fixture
def pcb_file():
    # load from text: kicad.loads caches (and shares) objects loaded by path
    return kicad.loads(kicad.pcb.PcbFile, PCBFILE.read_text())


@pytest.fixture
def pcb(pcb_file):
    # the parent PcbFile must stay referenced: child objects point into it
    return pcb_file.kicad_pcb


def _place(pcb, ref, x, y, r=0.0, layer="F.Cu"):
    fp = find_one_footprint(pcb, ref=ref)
    move_footprint(fp, x=x, y=y, r=r, layer=layer)
    return fp


def test_list_footprints(pcb):
    entries = list_footprints(pcb)
    refs = [e.reference for e in entries]
    assert "R1" in refs and "D1" in refs
    r1 = next(e for e in entries if e.reference == "R1")
    assert (r1.x, r1.y) == (68.232146, 84.772146)
    # fixture has no Edge.Cuts -> no outside flag
    assert r1.outside_outline is None


def test_list_footprints_like_filter(pcb):
    assert {e.reference for e in list_footprints(pcb, like="R*")} == {"R1"}
    # glob also matches against the footprint name
    assert {e.reference for e in list_footprints(pcb, like="*LED*")} == {"D1"}


def test_find_one_footprint_errors(pcb):
    with pytest.raises(PlacementError, match="No footprint"):
        find_one_footprint(pcb, ref="X99")
    with pytest.raises(PlacementError, match="Select a footprint"):
        find_one_footprint(pcb)


def test_move_footprint_absolute_and_relative(pcb):
    fp = find_one_footprint(pcb, ref="R1")
    move_footprint(fp, x=10, y=20, r=90)
    assert (fp.at.x, fp.at.y, fp.at.r) == (10, 20, 90)
    # pads carry the footprint rotation in kicad files
    assert all((pad.at.r or 0) % 360 in (90, 270) for pad in fp.pads)

    move_footprint(fp, dx=1.5, dy=-0.5)
    assert (fp.at.x, fp.at.y) == (11.5, 19.5)
    assert fp.at.r == 90


def test_move_footprint_flips_layer(pcb):
    fp = find_one_footprint(pcb, ref="R1")
    move_footprint(fp, layer="B.Cu")
    assert fp.layer == "B.Cu"
    assert all("B.Cu" in pad.layers for pad in fp.pads)


def test_remove_footprint(pcb):
    fp = find_one_footprint(pcb, ref="R1")
    n = len(pcb.footprints)
    remove_footprint(pcb, fp)
    assert len(pcb.footprints) == n - 1
    with pytest.raises(PlacementError, match="No footprint"):
        find_one_footprint(pcb, ref="R1")


def test_symmetry_pairs_and_deviation(pcb):
    # D1/R1 share no footprint, so pair R1 against a moved twin of D1:
    # place D1 and R1 as an intentionally imperfect mirror pair of... not
    # possible with different footprints -> use two footprints of same type.
    # The fixture only has one R0402, so test with D1 mirrored onto itself
    # being centered, and R1 unpaired.
    _place(pcb, "D1", AXIS - 10, 30, r=90)
    _place(pcb, "R1", AXIS - 20, 40)

    report = symmetry_report(pcb, axis=AXIS, include="D*,R*")
    assert report.axis == AXIS
    assert not report.pairs
    assert {e.reference for e in report.unpaired} == {"D1", "R1"}


def test_symmetry_centered_snap(pcb):
    _place(pcb, "B1", AXIS + 0.3, 40)
    report = symmetry_report(pcb, axis=AXIS, include="B1")
    assert [(e.reference, round(off, 3)) for e, off in report.centered] == [
        ("B1", 0.3)
    ]

    changes = apply_symmetry_fix(pcb, report)
    assert len(changes) == 1
    fp = find_one_footprint(pcb, ref="B1")
    assert fp.at.x == AXIS


def test_symmetry_pair_fix_keep_left(pcb):
    _place(pcb, "R1", AXIS - 10, 30, r=90)

    # duplicate R1 by loading a second copy of the file
    # (zig-backed objects don't support deepcopy)
    donor_file = kicad.loads(kicad.pcb.PcbFile, PCBFILE.read_text())
    twin = find_one_footprint(donor_file.kicad_pcb, ref="R1")
    for p in twin.propertys:
        if p.name == "Reference":
            p.value = "R2"
    twin.uuid = "deadbeef-0000-0000-0000-000000000000"
    kicad.insert(pcb, "footprints", pcb.footprints, twin)

    # imperfect mirror: off by (0.3, -0.2), rotation -r convention
    _place(pcb, "R2", AXIS + 10 + 0.3, 30 - 0.2, r=270)

    report = symmetry_report(pcb, axis=AXIS, include="R*")
    assert len(report.pairs) == 1
    pair = report.pairs[0]
    assert (pair.left.reference, pair.right.reference) == ("R1", "R2")
    assert pair.dx == pytest.approx(0.3)
    assert pair.dy == pytest.approx(-0.2)
    assert pair.rot_relation == "-r"

    changes = apply_symmetry_fix(pcb, report, keep="left")
    assert len(changes) == 1
    r2 = find_one_footprint(pcb, ref="R2")
    assert r2.at.x == pytest.approx(AXIS + 10)
    assert r2.at.y == pytest.approx(30)
    assert r2.at.r == 270  # rotation untouched

    # left side untouched
    r1 = find_one_footprint(pcb, ref="R1")
    assert (r1.at.x, r1.at.y) == (AXIS - 10, 30)

    # now symmetric
    report = symmetry_report(pcb, axis=AXIS, include="R*")
    assert report.pairs[0].deviation == pytest.approx(0, abs=1e-9)


def test_symmetry_requires_axis_without_outline(pcb):
    with pytest.raises(PlacementError, match="no Edge.Cuts"):
        symmetry_report(pcb, axis=None)
