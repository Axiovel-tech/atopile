---
name: schematic-export
description: "How the KiCad schematic exporter works (PCB-derived IR -> rule-based placer -> .kicad_sch), the render-inspect-fix-rule iteration loop, validation via kicad-cli, and the format landmines that make KiCad reject generated files."
---

# Schematic Export

Generates a reviewable, hierarchical KiCad schematic from every build (the
`schematic` target, part of the default set). The schematic is a **projection
of the design graph** — `.ato` stays the single source of connectivity truth;
generated files say "do not treat as source" in the title block.

Output goes **next to the layout**, so the layout directory is a complete
KiCad project (`.kicad_pro` + `.kicad_sch` + `.kicad_pcb`). SVG renders land
in `build/builds/<target>/schematic_svg/`.

## Quick Start

```bash
ato build                       # writes <layout>/<name>.kicad_sch + sub-sheets
kicad-cli sch erc --severity-error --format json -o /tmp/erc.json \
    <layout-dir>/<name>.kicad_sch
kicad-cli sch export svg --output /tmp/svg <layout-dir>/<name>.kicad_sch
```

## Relevant Files

- `src/faebryk/exporters/schematic/from_pcb.py` — IR provider (v1): extracts
  sheets/components/nets/roles from the **built `.kicad_pcb`**
  (`atopile_address` properties = module hierarchy, pads = netlist) plus the
  project `parts/` dir (symbols). Also: symbol pin/bbox geometry extraction,
  legacy-graphics normalization, `SHEET_MIN_COMPONENTS` threshold.
- `src/faebryk/exporters/schematic/kicad_writer.py` — rule-based placer and
  `.kicad_sch` text emitter (`SheetWriter`), power-symbol synthesis,
  hierarchical sheet pins, junction inference, smallest-fit paper selection.
- `src/atopile/build_steps.py` — `generate_schematic` target (+ the
  always-written `.kicad_pro` in `update_pcb`).
- `src/faebryk/libs/kicad/sexp_tools.py` — tolerant lossless sexp parser used
  to carry symbol bodies verbatim.

## Layout Rules (where to change what)

| Behavior | Where |
|---|---|
| What becomes a sheet vs an inlined cluster | `from_pcb.SHEET_MIN_COMPONENTS` (>=8 components) + collapse in `build_ir` |
| Component role (anchor/series/pull/decoupling) | `from_pcb._classify_role` (pad-count + power-net heuristics) |
| Which anchor pin a satellite attaches to | `kicad_writer._plan_cluster` — shared net scored signal(3) > rail(2) > GND(1); GND-only matches go to the loose row |
| Satellite stacking/wire nesting | `kicad_writer._plan_anchor_block` — per-side stacks sorted by anchor-pin y |
| Power flag vs label decision | `_pin_termination`: flag only when the pin exits vertically in the conventional direction (GND down, rail up); everything else gets a label |
| Local vs hierarchical vs global label | `_label`: power -> global (pairs with flags); crossing sheet boundary -> hierarchical (matched by sheet pins, see `_child_ports`/`_place_sheet_boxes`); else local |
| Junction dots | `_emit_junctions`: 3+ wire ends meeting, or 2+ on a symbol pin |
| Paper size | `render`: lays out against A4..A1 widths, takes the smallest fit |

## Iteration Loop (the only loop that compounds)

Defects must be fixed in the **rules**, never in a generated file:

1. `ato build` (TSMINI and ESC boards are the test corpus)
2. render: `kicad-cli sch export svg` (PNG via `cairosvg` for inspection)
3. inspect against the reference question: "does the buck read like the
   TPS62913 application circuit?"
4. fix the rule in `kicad_writer.py` / `from_pcb.py`
5. regenerate — output is deterministic (uuid5 from stable keys), so diffs
   show exactly what the rule changed

## Invariants / Format Landmines

- **Determinism**: every UUID is `uuid5`-derived from stable keys
  (`_uid(...)`). Never introduce `uuid4`/time into emitted content;
  reproducible output is what makes schematics diffable and is the basis for
  the future position-sync layer.
- **Symbols carry `atopile_address`** (hidden property) — the key for
  position sync; do not remove.
- Symbol bodies are copied **verbatim** from the part's `.kicad_sym` except:
  legacy circles `(center)+(end)` must be rewritten to `(center)+(radius)`
  (`from_pcb._normalize_symbol_graphics`) — the schematic parser rejects the
  legacy form with a bare "Failed to load schematic".
- Inner unit names must stay `"<OuterName>_<unit>_<style>"` after renaming
  (`from_pcb._rename_symbol` handles this).
- Hierarchical labels in a child **must** have a matching `(pin ...)` on the
  sheet symbol in the parent (ERC validates); both are generated from the
  same `_child_ports` list — keep them in sync.
- KiCad connects coincident wire *endpoints* with or without a junction dot;
  a wire end touching another wire's *middle* is NOT connected. Junction
  inference exists for visual disambiguation — geometry rules should still
  avoid mid-segment touches.
- Debugging "Failed to load schematic" (kicad-cli prints no detail): bisect
  by splicing suspect elements into a minimal `(kicad_sch ...)` skeleton and
  re-testing — see the parser version-gating note in the `layout-tools`
  skill (same failure mode exists for boards).

## Roadmap (agreed direction)

Rules own structure; agent tools own the last mile, gated on:
1. `schematic check` — machine-readable overlap/crossing/collision report
   (feedback signal for both rule-tuning and agent review)
2. Position sync (L2) — read symbol positions back by `atopile_address`
   before regenerating, so eeschema/agent edits survive rebuilds (mirror of
   the PCB layout-sync contract)
3. Graph-based IR provider — interface direction and `~>` bridge chains for
   flow-aware placement and real in/out port shapes (current provider is
   PCB-derived and direction-blind; ports are `passive`)
4. Netlist-divergence check — diff KiCad's exported netlist against the ato
   netlist so manual schematic edits are flagged loudly
