# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

from faebryk.libs.kicad.fileformats import kicad
from faebryk.libs.kicad.kicad10_compat import upgrade_kicad10_pcb_text

KICAD10_PCB = """(kicad_pcb
  (version 20260306)
  (generator "pcbnew")
  (generator_version "10.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal))
  (footprint "test:FP"
    (layer "F.Cu")
    (uuid "11111111-2222-3333-4444-555555555555")
    (at 100 100)
    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS")
      (uuid "aaaa1111-2222-3333-4444-555555555555")
      (effects (font (size 1.27 1.27))))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "VCC"))
    (zone
      (layers "*.Cu")
      (uuid "bbbb1111-2222-3333-4444-555555555555")
      (name "antenna_keepout")
      (hatch edge 0.5)
      (keepout (tracks not_allowed) (vias not_allowed) (pads not_allowed)
        (copperpour not_allowed) (footprints not_allowed))
      (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))
      (polygon (pts (xy -1 -1) (xy 1 -1) (xy 1 1) (xy -1 1)))
    )
  )
  (segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net "VCC"))
)
"""


def test_upgrades_footprint_embedded_zone():
    """KiCad 10 rule areas inside footprints get v9-style net references."""
    upgraded = upgrade_kicad10_pcb_text(KICAD10_PCB)

    pcb = kicad.loads(kicad.pcb.PcbFile, upgraded).kicad_pcb
    fp = pcb.footprints[0]
    assert len(fp.zones) == 1
    zone = fp.zones[0]
    assert zone.name == "antenna_keepout"
    assert zone.net == 0
    assert zone.keepout is not None
    assert str(zone.keepout.tracks) == "not_allowed"

    # net table synthesized, pad reference numbered
    assert any(n.name == "VCC" for n in pcb.nets)
    assert fp.pads[0].net is not None
    assert fp.pads[0].net.name == "VCC"


def test_non_kicad10_text_unchanged():
    text = "(kicad_pcb (version 20241229))"
    assert upgrade_kicad10_pcb_text(text) is text
