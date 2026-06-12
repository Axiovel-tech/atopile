---
name: layout-tools
description: "Board-level tooling: offline part creation (part-from-kicad / part-from-pinout), placement adoption from hand-laid boards, board-outline/copper-layers/design-rules config, and the agent routing loop (ratsnest -> zones -> fanout -> apply-routes -> kicad-cli DRC). Includes the KiCad 10 compatibility contract."
---

# Layout Tools

The toolchain that takes an ato design from netlist to a routed board, built
to be drivable by agents: every step is a CLI with machine-readable output,
validated by `kicad-cli pcb drc`.

## Quick Start (the routing loop)

```bash
ato build                                          # netlist -> layout synced
ato layout ratsnest --unrouted                     # per-net pad coords to plan with
ato layout add-zone --net GND --layer In1.Cu --name L2_GND
ato layout fanout --net GND                        # stitching vias next to every pad
ato layout apply-routes plan.json                  # tracks/vias/zones from JSON
kicad-cli pcb drc --severity-error --refill-zones --save-board \
    --format json -o /tmp/drc.json <layout>.kicad_pcb
```

`plan.json` schema: see `apply_route_plan` docstring in
`src/faebryk/exporters/pcb/routing_tools.py`.

## Offline Part Creation

The EasyEDA download path is not always reachable; both commands below are
fully local:

```bash
# from a local KiCad library (company lib or /usr/share/kicad):
ato create part-from-kicad -y lib.kicad_sym -n SYMBOL \
    -f lib.pretty/FP.kicad_mod -m "Manufacturer" --partnumber PN \
    [--supplier-partno C123]

# from a datasheet pinout (symbol synthesized as a box):
ato create part-from-pinout -f pkg.kicad_mod \
    --pin 1:GVDD --pin 2:GND ... -m TI --partnumber DRV8300DRGE
```

Implementation: `src/faebryk/libs/kicad_part_import.py` (tolerant symbol-lib
parsing via `src/faebryk/libs/kicad/sexp_tools.py`, legacy `(module ...)`
footprints converted through the v5 model). CLI: `src/atopile/cli/create.py`.

## Placement Adoption (migration from hand-laid boards)

```bash
ato layout adopt-placement --from donor.kicad_pcb [--dry-run]
```

Matches footprints by library base name; repeated types are assigned by a
pad net-name fingerprint. Side flips are geometry-correct
(`PCB_Transformer.move_fp`). Implementation:
`src/faebryk/exporters/pcb/layout/adopt_placement.py`.

## Footprint Placement (works on bare `.kicad_pcb`, no ato project needed)

```bash
ato layout fps --pcb board.kicad_pcb [--like "LED*"] [--json]
ato layout place C1 --x 220.0 --y 107.1 --rot 90 [--layer B.Cu] --pcb board.kicad_pcb
ato layout place --json '[{"ref":"C1","x":1,"y":2,"rot":0,"layer":"B.Cu"}, ...]' --pcb ...
ato layout remove LED2 LED7 [--dry-run] --pcb board.kicad_pcb
ato layout symmetry --include "LED*,MountingHole*" [--axis 232.6] [--fix --keep left] --pcb ...
```

- `fps` lists reference/value/position/rotation/layer (+uuid prefix) and flags
  footprints outside the outline **bbox** (staircase outlines: cutout regions
  are not detected — verify visually with `kicad-cli pcb render`).
- `place` accepts batch moves as JSON (inline or file). Layer change flips
  geometry-correct. Non-unique references (e.g. `H**` mounting holes):
  select with `--uuid <prefix>`.
- `symmetry` derives the mirror axis from the outline bbox, verifies the
  Edge.Cuts primitives mirror each other, pairs footprints per
  (footprint, layer) group, and reports per-pair deviation plus the rotation
  relation (`-r`, `180-r`, `none`). `--fix --keep left|right` snaps positions
  (never rotations) to the exact mirror.

Implementation: `src/faebryk/exporters/pcb/placement_tools.py`; CLI in
`src/atopile/cli/layout.py`; tests in
`test/exporters/pcb/test_placement_tools.py`.

## Board Config (ato.yaml, per build target)

```yaml
builds:
  my_board:
    entry: main.ato:MyBoard
    copper-layers: 6              # inner layers added on build
    design-rules:                 # written into <layout>.kicad_pro
      min-copper-edge-clearance: 0.05   # e.g. edge-mounted side-view LEDs
    board-outline:
      rounded-rect: {x: 212.2, y: 86.0, width: 38.1, height: 37.5, radius: 1.0}
      # or polygon: [{at: [x, y], fillet: r}, ...]
```

- Outline: `src/faebryk/exporters/pcb/outline.py` (polygon + per-vertex
  tangent-arc fillets); regenerated idempotently (uuid-marked edges).
- Config models: `BoardOutlineConfig` / `DesignRulesConfig` in
  `src/atopile/config.py`; applied in `update_pcb`
  (`src/atopile/build_steps.py`). The `.kicad_pro` is written on **every**
  build (full-project contract), design rules merged in when configured.

## Invariants

- **Ownership contract**: `.ato` owns connectivity/BOM; the `.kicad_pcb`
  owns geometry (placement/routing) and is synced, never clobbered
  (`src/faebryk/exporters/pcb/layout/layout_sync.py`). All *generated*
  copper/edges are uuid-marked (`PCB_Transformer.gen_uuid(mark=True)`) so
  they can be replaced/cleared (`--clear-generated`) without touching
  user-drawn objects.
- **atopile writes KiCad 9 style board files** (version 20241229). KiCad 10
  reads them transparently. Boards saved by KiCad 10 are upgraded at load by
  `src/faebryk/libs/kicad/kicad10_compat.py` (synthesizes the net table from
  name-only refs — including rule-area zones embedded in footprints — and
  strips v10-only setup tokens). Never model v10-only setup
  tokens (`covering`/`plugging`/`capping`/`filling`) in the Zig sexp structs:
  emitting them into a v9-stamped document makes KiCad reject the file —
  KiCad's parser is version-gated per token.
- **Schematics have no such shim**: the Zig schematic model targets the
  v6-era grammar (20211123) and fails on KiCad 9/10 `.kicad_sch` files
  (e.g. `(pin_numbers (hide yes))`). Don't load modern schematics through
  `kicad.loads`; edit them with targeted text surgery and validate with
  `kicad-cli sch export netlist` / `kicad-cli sch erc`.
- DRC verdicts: always run `kicad-cli pcb drc` with `--refill-zones`;
  without it, stale-fill artifacts produce hundreds of phantom violations.
- Fanout vias default 0.47/0.25 — matches JLC capability but exceeds KiCad's
  default rules; a `design-rules:` block is required for clean DRC.

## Dependants (Call Sites)

- CLI: `src/atopile/cli/layout.py` (`ato layout ...` group, registered in
  `src/atopile/cli/cli.py`)
- Build: `update_pcb` in `src/atopile/build_steps.py` (outline, layers,
  design rules); `generate_schematic` consumes the resulting layout.
