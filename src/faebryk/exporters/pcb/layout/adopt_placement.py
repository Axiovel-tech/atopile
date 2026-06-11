# This file is part of the faebryk project
# SPDX-License-Identifier: MIT

"""
Adopt component placement from a donor `.kicad_pcb` into an atopile layout.

This is the migration path for boards that were (partially) laid out by hand
in KiCad before the design was ported to atopile: footprints in the generated
layout are matched against the donor board and moved to the donor positions.

Matching is done per footprint type (library base name). Unique footprints
match directly; for repeated footprints (passives), candidates are assigned
greedily by a net-name fingerprint score, so e.g. the decoupling cap sitting
on +3V3/GND in the donor ends up at the +3V3/GND position in the new layout.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from faebryk.libs.kicad.fileformats import kicad

logger = logging.getLogger(__name__)


def _base_name(fp_name: str) -> str:
    return fp_name.split(":")[-1]


def _normalize_net(name: str | None) -> str | None:
    if name is None or name == "":
        return None
    # strip KiCad hierarchical sheet prefixes ("/sheet/NET" -> "NET")
    name = name.rsplit("/", 1)[-1]
    return name.upper()


@dataclass
class FootprintInfo:
    index: int
    reference: str
    base_name: str
    at_x: float
    at_y: float
    at_r: float
    layer: str
    #: pad name -> normalized net name
    pad_nets: dict[str, str | None] = field(default_factory=dict)


@dataclass
class AdoptionResult:
    #: (target_ref, donor_ref, score)
    matched: list[tuple[str, str, float]] = field(default_factory=list)
    #: target refs with no donor candidate
    unmatched_targets: list[str] = field(default_factory=list)
    #: donor refs that were not used
    unused_donors: list[str] = field(default_factory=list)
    #: matches where the footprint also changed board side
    side_changes: list[str] = field(default_factory=list)


def _collect_footprints(pcb: "kicad.pcb.PcbFile") -> list[FootprintInfo]:
    out: list[FootprintInfo] = []
    for i, fp in enumerate(pcb.kicad_pcb.footprints):
        ref = next(
            (p.value for p in fp.propertys if p.name == "Reference"), f"#{i}"
        )
        info = FootprintInfo(
            index=i,
            reference=ref,
            base_name=_base_name(fp.name),
            at_x=fp.at.x,
            at_y=fp.at.y,
            at_r=fp.at.r or 0,
            layer=fp.layer,
        )
        for pad in fp.pads:
            info.pad_nets[pad.name] = _normalize_net(
                pad.net.name if pad.net else None
            )
        out.append(info)
    return out


def _fingerprint_score(target: FootprintInfo, donor: FootprintInfo) -> float:
    """Fraction of pads whose (normalized) net names agree."""
    if not target.pad_nets:
        return 0.0
    hits = 0
    total = 0
    for pad_name, t_net in target.pad_nets.items():
        d_net = donor.pad_nets.get(pad_name)
        if t_net is None and d_net is None:
            continue
        total += 1
        if t_net is not None and t_net == d_net:
            hits += 1
    if total == 0:
        return 0.0
    return hits / total


def _match_group(
    targets: list[FootprintInfo], donors: list[FootprintInfo]
) -> list[tuple[FootprintInfo, FootprintInfo, float]]:
    """Greedy best-score assignment within one footprint-type group."""
    pairs: list[tuple[float, FootprintInfo, FootprintInfo]] = []
    for t in targets:
        for d in donors:
            pairs.append((_fingerprint_score(t, d), t, d))
    # highest score first; deterministic tie-break by reference
    pairs.sort(key=lambda p: (-p[0], p[1].reference, p[2].reference))

    used_t: set[int] = set()
    used_d: set[int] = set()
    out: list[tuple[FootprintInfo, FootprintInfo, float]] = []
    for score, t, d in pairs:
        if t.index in used_t or d.index in used_d:
            continue
        used_t.add(t.index)
        used_d.add(d.index)
        out.append((t, d, score))
    return out


def adopt_placement(
    target_pcb_path: Path,
    donor_pcb_path: Path,
    *,
    dry_run: bool = False,
    only_refs: set[str] | None = None,
) -> AdoptionResult:
    """
    Match footprints in `target_pcb_path` against `donor_pcb_path` and apply
    the donor's position/rotation/side to each match.
    """
    target_pcb = kicad.loads(kicad.pcb.PcbFile, target_pcb_path)
    donor_pcb = kicad.loads(kicad.pcb.PcbFile, donor_pcb_path)

    targets = _collect_footprints(target_pcb)
    donors = _collect_footprints(donor_pcb)

    if only_refs:
        targets = [t for t in targets if t.reference in only_refs]

    targets_by_name: dict[str, list[FootprintInfo]] = defaultdict(list)
    donors_by_name: dict[str, list[FootprintInfo]] = defaultdict(list)
    for t in targets:
        targets_by_name[t.base_name].append(t)
    for d in donors:
        donors_by_name[d.base_name].append(d)

    result = AdoptionResult()
    matches: list[tuple[FootprintInfo, FootprintInfo, float]] = []

    for name, t_group in targets_by_name.items():
        d_group = donors_by_name.get(name, [])
        if not d_group:
            result.unmatched_targets.extend(t.reference for t in t_group)
            continue
        group_matches = _match_group(t_group, d_group)
        matches.extend(group_matches)
        matched_t = {t.index for t, _, _ in group_matches}
        result.unmatched_targets.extend(
            t.reference for t in t_group if t.index not in matched_t
        )

    used_donor_idx = {d.index for _, d, _ in matches}
    result.unused_donors = [
        d.reference for d in donors if d.index not in used_donor_idx
    ]

    from faebryk.exporters.pcb.kicad.transformer import PCB_Transformer

    for t, d, score in sorted(matches, key=lambda m: m[0].reference):
        result.matched.append((t.reference, d.reference, score))

        fp = target_pcb.kicad_pcb.footprints[t.index]
        if fp.layer != d.layer:
            result.side_changes.append(t.reference)

        # move_fp handles side flips (geometry mirroring + layer swap),
        # rotation deltas (pad rotations are absolute in kicad files) and
        # the final position
        PCB_Transformer.move_fp(
            fp,
            kicad.pcb.Xyr(x=d.at_x, y=d.at_y, r=d.at_r or None),
            d.layer,
        )

    if not dry_run:
        kicad.dumps(target_pcb, target_pcb_path)

    return result
