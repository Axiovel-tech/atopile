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
  name-only refs, strips v10-only setup tokens). Never model v10-only setup
  tokens (`covering`/`plugging`/`capping`/`filling`) in the Zig sexp structs:
  emitting them into a v9-stamped document makes KiCad reject the file —
  KiCad's parser is version-gated per token.
- DRC verdicts: always run `kicad-cli pcb drc` with `--refill-zones`;
  without it, stale-fill artifacts produce hundreds of phantom violations.
- Fanout vias default 0.47/0.25 — matches JLC capability but exceeds KiCad's
  default rules; a `design-rules:` block is required for clean DRC.

## Dependants (Call Sites)

- CLI: `src/atopile/cli/layout.py` (`ato layout ...` group, registered in
  `src/atopile/cli/cli.py`)
- Build: `update_pcb` in `src/atopile/build_steps.py` (outline, layers,
  design rules); `generate_schematic` consumes the resulting layout.
