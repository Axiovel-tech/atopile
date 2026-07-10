# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

import math

import pytest

from faebryk.libs.kicad.board_ops import (
    Placement,
    apply_placement,
    check_courtyard_overlaps,
    copy_setup_text,
    courtyard_bboxes,
    dump_placement,
    set_rectangular_outline,
    summarize,
)
from faebryk.libs.kicad.fileformats import kicad

_MINIMAL_PCB = """
(kicad_pcb
    (version 20241229)
    (generator "test_board_ops")
    (generator_version "latest")
    (layers
        (0 "F.Cu" signal)
        (2 "B.Cu" signal)
    )
)
"""

_FOOTPRINT_PCB = """
(kicad_pcb
    (version 20241229)
    (generator "test_board_ops")
    (generator_version "latest")
    (layers
        (0 "F.Cu" signal)
        (2 "B.Cu" signal)
    )
    (net 0 "")
    (net 1 "GND")
    (footprint "Test:R1"
        (layer "F.Cu")
        (at 10 10 0)
        (property "Reference" "R1"
            (at 0 -2 0)
            (layer "F.SilkS")
        )
        (fp_rect
            (start -1 -0.6)
            (end 1 0.6)
            (stroke (width 0.05) (type solid))
            (fill no)
            (layer "F.CrtYd")
        )
        (pad "1" smd rect
            (at -0.8 0)
            (size 0.9 1)
            (layers "F.Cu")
            (net 1 "GND")
        )
        (pad "2" smd rect
            (at 0.8 0 90)
            (size 0.9 1)
            (layers "F.Cu")
        )
    )
    (footprint "Test:R2"
        (layer "F.Cu")
        (at 11.5 10 0)
        (property "Reference" "R2"
            (at 0 -2 0)
            (layer "F.SilkS")
        )
        (fp_rect
            (start -1 -0.6)
            (end 1 0.6)
            (stroke (width 0.05) (type solid))
            (fill no)
            (layer "F.CrtYd")
        )
        (pad "1" smd rect
            (at -0.8 0)
            (size 0.9 1)
            (layers "F.Cu")
        )
    )
    (footprint "Test:R3"
        (layer "B.Cu")
        (at 11.5 10 0)
        (property "Reference" "R3"
            (at 0 -2 0)
            (layer "B.SilkS")
        )
        (fp_rect
            (start -1 -0.6)
            (end 1 0.6)
            (stroke (width 0.05) (type solid))
            (fill no)
            (layer "B.CrtYd")
        )
    )
)
"""


def _load(text: str):
    return kicad.loads(kicad.pcb.PcbFile, text)


def test_outline_rect_roundtrip():
    pcb = _load(_MINIMAL_PCB)
    set_rectangular_outline(pcb, width_mm=110, height_mm=80, corner_radius_mm=3)
    assert len([g for g in pcb.kicad_pcb.gr_lines if g.layer == "Edge.Cuts"]) == 4
    assert len([g for g in pcb.kicad_pcb.gr_arcs if g.layer == "Edge.Cuts"]) == 4

    summary = summarize(pcb)
    assert summary.size_mm == (110.0, 80.0)

    # re-parse what we serialize
    pcb2 = _load(kicad.dumps(pcb))
    assert summarize(pcb2).size_mm == (110.0, 80.0)

    # replacing wipes the old outline instead of stacking
    set_rectangular_outline(pcb, width_mm=50, height_mm=40)
    assert summarize(pcb).size_mm == (50.0, 40.0)
    assert len([g for g in pcb.kicad_pcb.gr_arcs if g.layer == "Edge.Cuts"]) == 0


def test_outline_validation():
    pcb = _load(_MINIMAL_PCB)
    with pytest.raises(ValueError):
        set_rectangular_outline(pcb, width_mm=0, height_mm=10)
    with pytest.raises(ValueError):
        set_rectangular_outline(pcb, width_mm=10, height_mm=10, corner_radius_mm=6)


def test_summarize_counts():
    summary = summarize(_load(_FOOTPRINT_PCB))
    assert summary.footprints == 3
    assert summary.footprints_front == 2
    assert summary.footprints_back == 1
    assert summary.nets == 2
    assert summary.pads_total == 3
    assert summary.pads_unconnected == 2
    assert summary.copper_layers == ["F.Cu", "B.Cu"]


def test_copy_setup_text_with_rename():
    donor = """
    (kicad_pcb
        (version 20241229)
        (generator "donor")
        (generator_version "latest")
        (layers
            (0 "F.Cu" signal "L1_SIG")
            (4 "In1.Cu" signal "L2_GND")
            (2 "B.Cu" signal "L6_SIG")
        )
        (setup
            (pad_to_mask_clearance 0)
        )
    )
    """
    out = copy_setup_text(_MINIMAL_PCB, donor, rename_layers={"L2_GND": "L2_PWR"})
    pcb = _load(out)
    assert len(pcb.kicad_pcb.layers) == 3
    assert '"L2_PWR"' in out
    assert '"L2_GND"' not in out
    assert pcb.kicad_pcb.setup.pad_to_mask_clearance == 0

    with pytest.raises(ValueError):
        copy_setup_text(_MINIMAL_PCB, donor, rename_layers={"NOPE": "X"})


def test_placement_roundtrip_and_rotation():
    pcb = _load(_FOOTPRINT_PCB)
    placement = dump_placement(pcb)
    assert placement["R1"].x == 10 and placement["R1"].rotation == 0

    placement["R1"] = Placement(x=20, y=30, rotation=90, layer="F.Cu")
    applied = apply_placement(pcb, {"R1": placement["R1"]})
    assert applied == ["R1"]

    fp = next(
        f
        for f in pcb.kicad_pcb.footprints
        if any(p.value == "R1" for p in f.propertys if p.name == "Reference")
    )
    assert (fp.at.x, fp.at.y, fp.at.r) == (20, 30, 90)
    # pad angles pick up the rotation delta; positions stay relative
    pad1 = next(p for p in fp.pads if p.name == "1")
    pad2 = next(p for p in fp.pads if p.name == "2")
    assert (pad1.at.r or 0) == 90
    assert (pad2.at.r or 0) == 180
    assert math.isclose(pad1.at.x, -0.8)

    # absolute courtyard follows the rotation: R1 rect becomes tall
    boxes = courtyard_bboxes(pcb)
    x0, y0, x1, y1 = boxes["R1"]
    assert math.isclose(x1 - x0, 1.2, abs_tol=1e-6)
    assert math.isclose(y1 - y0, 2.0, abs_tol=1e-6)

    with pytest.raises(KeyError):
        apply_placement(pcb, {"R99": Placement(x=0, y=0)})
    assert apply_placement(pcb, {"R99": Placement(x=0, y=0)}, strict=False) == []
    with pytest.raises(NotImplementedError):
        apply_placement(pcb, {"R1": Placement(x=0, y=0, layer="B.Cu")})


def test_courtyard_overlaps_same_side_only():
    pcb = _load(_FOOTPRINT_PCB)
    # R1 at x=10, R2 at x=11.5: courtyards are 2 mm wide -> 0.5 mm x 1.2 mm
    overlaps = check_courtyard_overlaps(pcb)
    pairs = {(o.ref_a, o.ref_b) for o in overlaps}
    assert pairs == {("R1", "R2")}  # R3 overlaps R2 in xy but is on B.Cu
    assert math.isclose(overlaps[0].area_mm2, 0.6, abs_tol=1e-3)

    # min-area filter suppresses it
    assert check_courtyard_overlaps(pcb, min_area_mm2=1.0) == []

    # moving R2 away clears the overlap
    apply_placement(pcb, {"R2": Placement(x=20, y=10)})
    assert check_courtyard_overlaps(pcb) == []


def test_kicad10_board_loads_transparently():
    """KiCad 10 constructs (nested tenting, name-only pad nets) load via the
    kicad10_compat shim wired into `kicad.loads`."""
    from faebryk.libs.kicad.board_ops import load_board_text

    v10 = """
(kicad_pcb
    (version 20260206)
    (generator "pcbnew")
    (generator_version "10.0")
    (layers
        (0 "F.Cu" signal)
    )
    (setup
        (pad_to_mask_clearance 0)
        (tenting
            (front yes)
            (back yes)
        )
        (covering
            (front no)
            (back no)
        )
    )
    (footprint "Test:R1"
        (layer "F.Cu")
        (at 10 10 0)
        (property "Reference" "R1"
            (at 0 -2 0)
            (layer "F.SilkS")
        )
        (pad "1" smd rect
            (at -0.8 0)
            (size 0.9 1)
            (layers "F.Cu")
            (net "GND")
        )
        (pad "2" smd rect
            (at 0.8 0)
            (size 0.9 1)
            (layers "F.Cu")
            (net "GND")
        )
    )
)
"""
    pcb = load_board_text(v10)
    net_names = {n.name for n in pcb.kicad_pcb.nets}
    assert "GND" in net_names
    fp = pcb.kicad_pcb.footprints[0]
    nums = {p.net.number for p in fp.pads if p.net is not None}
    assert len(nums) == 1 and 0 not in nums
