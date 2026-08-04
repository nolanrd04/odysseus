# DWG Extraction Field Rules

High-stakes rules for the DWG → quantity-sheet extraction agent. Every rule below was paid for with a
real investigation mistake or hard-won finding in the `dwg_to_qty_sheet`
sessions — they are constraints, not suggestions. Adjust only as live testing
surfaces gaps.

## Investigation discipline

1. **Read the census before touching geometry.** The full layer/block/attribute
   census has already been run for you and is in your context. Do not extract
   anything from a layer you have not located in the census, and treat census
   layers you cannot explain as open questions, not noise. (Sessions kept
   finding "the technique existed, it just wasn't applied" — the census is how
   you find what applies.)

2. **Inspect individual entities before trusting any flat per-layer or
   per-hatch sum.** Two documented EXPO findings that looked "unusable" or
   "unexplained" were investigation bugs from trusting a layer-level total
   instead of examining the entities underneath it. Sample real entities
   (coordinates, areas, closed/open flags) before aggregating.

3. **Never trust a single heuristic — cross-check.** Vertex counts, plan-sheet
   callout text, layer names, and attribute patterns have each been wrong at
   least once in this project's history. The EXPO pedestrian-ramp count from a
   keynote callout (2) was wrong; the DXF hatch-pattern attribute (`HEX` → 6)
   was right. When two signals disagree, investigate both; when you have only
   one signal, say so in your output.

4. **A centerline/alignment layer can hold multiple unrelated features.** The
   completeness check in `centerline.py` is known to false-positive on wider
   ROWs. Verify what a centerline actually runs along before measuring it, and
   never assume "one layer = one feature."

5. **Count blocks by (name, attribute values), not name alone.** Several
   qty-sheet subtypes are one generic block distinguished only by an attribute
   value (sign type, drywell size, valve size). The census's
   `block_attributes` section flags these. Conversely, two different block
   names on one layer can be different billed items — "one layer = one
   countable thing" cannot be assumed.

6. **Anonymous blocks (`*U###`) have three distinct outcomes** — `resolved`
   (real dynamic block, use the true name), `empty_placeholder` (unresolved
   XREF stand-in, not countable geometry), and `unresolved` (real geometry,
   no resolution — investigate manually). Never treat a resolution failure as
   one uniform case, and never count an unresolved anonymous block as zero
   without checking its entities.

7. **`ACAD_PROXY_ENTITY` layers are recoverable, not dead.** Civil3D-native
   jobs convert key layers to proxy entities. Recover their real geometry via
   ezdxf's `virtual_entities()` (proven in session 2026_7_28.5) before
   declaring a layer unusable.

8. **HATCH areas: run the disjoint-loop sum-vs-net test.** Treating
   touching-but-not-overlapping panels as one merged shape produced a
   documented 31% undercount. Use `src.dwg_qty.area.hatch_area()` — its docstring
   records the fix — rather than summing `HATCH` areas by hand. Hatch
   `pattern_name` can be a real classifier (it separated ped ramps from plain
   concrete once), but rule 3 applies: cross-check it.

9. **Paperspace layouts (and sometimes modelspace) can hold literal text that
   proxy annotation doesn't — check there before calling a size/depth/width
   unrecoverable.** `DIMENSION` entities cache their exact rendered string in
   a private anonymous `*D####` block's `MTEXT`, independent of raw
   scale-factor math — this is where typical-section values (ROW/road/
   sidewalk/swale/easement width) usually live. `MULTILEADER` entities carry
   either a free-text spec callout (`has_mtext_content`) or a keynote-circle
   block reference with attribute overrides like `TAGNUMBER`
   (`has_block_content`). Use `src.dwg_qty.annotation`'s
   `read_dimension_texts()` / `read_multileader_contents()` — check the
   census's `annotation_layouts` field first to see which sheets have this
   content, and note that it is not paperspace-only (one corpus job had
   `MULTILEADER` entities in modelspace). Only about a third of firms checked
   so far annotate this way; a genuine empty `annotation_layouts` is real
   absence for that job, not a search failure — say so rather than treating
   it as ungrounded.

   The same module has three more targeted readers, each validated on
   Woodman but firm-specific in what layer to point them at — inspect the
   census/a raw MTEXT dump first, don't assume the layer names below exist
   on a new job:
   - `keynote_legend_sections()` — pairs a bare legend header TEXT
     ("SEWER KEYNOTES", "NOTES", etc.) with its nearest MTEXT body by
     position, resolving what a `MULTILEADER`'s `TAGNUMBER` actually means.
     Pass `layer=` (e.g. Woodman's `C-ANNO`) or it will also pair unrelated
     title-block fields as false-positive headers.
   - `read_lot_records()` — parses a "`<lot#>`\\n`<area>` S.F." MTEXT
     pattern into real platted lot count/area — literal ground truth for
     cross-checking service/meter counts against, not an inferred count.
   - `read_station_labels()` — pulls every `XX+YY.YY` civil-station token
     out of a layer's text, for a direct per-feature count (e.g. one label
     per service tap) instead of trusting endpoint-clustering, which is
     known to undercount when two runs' endpoints land in the same
     tolerance bucket.

## Using the library

10. **Prefer `src.dwg_qty` functions over reimplementing.** The signatures and
   docstrings in your context encode empirically-validated techniques,
   including their known failure modes. If a function's behavior surprises
   you, read its real implementation with `inspect.getsource(...)` before
   working around it.

11. **Gaps are expected — write code for them, and say you did.** When no
    `src.dwg_qty` function covers a case, write your own script (that is the
    intended escape hatch). Clearly mark in your final output which quantities
    came from ad hoc code rather than validated library functions, so a human
    can decide whether the technique should be promoted into the library.

12. **Batch checks into fewer, larger scripts — don't spend a round per data
    point.** Verifying one hatch, one layer, or one property per `python` call
    burns rounds fast on a job with dozens of layers, and your round budget is
    finite. Write one script that loops over every layer/entity you need to
    check for a given work type and prints a structured summary, rather than
    probing them one at a time across many separate calls.

13. **Every `python` call starts a brand-new, empty process — nothing you
    define persists between calls, except `doc`, `msp`, and `dq`.** Those
    three are pre-loaded automatically before your script runs (the DXF is
    already open, `src.dwg_qty` is already imported as `dq`) — do not spend a
    round re-opening the DXF or re-importing `dwg_qty`, they're already
    there. Everything else — any variable, function, or intermediate result
    your own code defines — is gone in the next call; referencing it raises
    `NameError` and wastes the whole round. When a result is expensive to
    recompute (a filtered entity list, a specific hatch-area total you'll
    need again), `write_file` it as JSON into the job folder and have a
    later script check for and load that cached file before recomputing.

14. **Narrate as you work. Before your first tool call, state in one sentence
    what you're about to investigate.** After each meaningful result — an
    analog job identified, a quantity resolved, a heuristic that disagrees
    with another, a gap confirmed genuinely unrecoverable — say it in a real
    reply *before* your next tool call. A `#` comment in your `python` code
    is not a report to the user, it is invisible to them; do not write
    findings there, and do not silently queue them for the final answer.
    Several rounds of nothing but tool calls with no reply reads as stalled,
    not efficient. Brief is good, silent is not — one short sentence per
    finding beats a wall of code comments and no reply at all.
    **This applies for the whole extraction, not just once you're near the
    end**: the general agent rule elsewhere about giving "one short
    confirmation... unless more work remains" does not apply to this flow —
    during a DWG extraction, more work remaining is the normal state for
    most of the turn, and it is never a reason to go quiet.

## Firm conventions (ghost-firm archetypes)

15. **Never assume what a layer-naming convention means — infer it from this
    job's own data.** No prefix scheme is hardcoded anywhere in this pipeline,
    deliberately. Characterize the job's convention (prefix scheme,
    proposed-vs-existing signals, apparent CAD-product template style) from
    the census, then verify your characterization against actual entities
    before relying on it.

16. **Ground your mapping in analog jobs, not cold judgment.** Use the
    `dwg_corpus_lookup` tool to fetch past jobs' census fingerprints and
    completed qty sheets; judge which past job's convention most resembles
    this one and use its completed sheet as few-shot grounding for how
    geometry resolved into Terra's template rows. Similarity is behavioral
    (what the census shows), never a firm name.

## Output contract

17. **The deliverable is Terra's qty_tbl table** (`General plan group` /
    `Work type` / `Unit` / `QTY` / `Notes`) — a **closed vocabulary** of row
    labels, not a fixed row count. Your final table must be **compact: only
    rows where you found a real QTY.** The blank fill-in template (below
    your library reference) is a reference checklist of exact labels/
    qualifiers to pull from while you work, not the literal shape of your
    final answer — do NOT reproduce all ~200 template rows, blank or not, in
    your response; a row with no evidence goes in the Case B list (rule 18)
    instead, never as a blank row in the table. When a row IS populated,
    reproduce its `Work type` string **verbatim**, including any
    depth-range, size, or SINGLE/DOUBLE-split suffix (e.g. `SEWER MAIN ( 8"
    ) - 7'-11' DEPTH`, `WATER SERVICES ( 1" ) - SINGLE`) — these qualifiers
    are part of the row identity, not decoration, and a paraphrased or
    merged label is scored as a complete miss even when the quantity is
    correct. Never invent a row/label outside the template (that's Case A);
    never guess a qualifier you have no evidence for (Case B instead of
    picking one arbitrarily).

18. **Flag, never silently resolve (Case A / Case B).**
    - **Case A — unclaimed evidence:** real census/DWG evidence (a named
      block, a hatch with real area, an attribute-flagged entity) that no
      template row claims → list it as "possible extra, no template row"
      with the evidence. Never silently discard it.
    - **Case B — unclaimed row:** a template row you found no evidence for →
      omit it from the final table (rule 17) and list it in your Case B
      summary as "possible miss" with what you looked for. A miss is
      indistinguishable from genuinely-N/A without ground truth — a human
      confirms every one; your job is to make that review fast, which a
      ~200-row table of blanks does not do.

19. **Report and stop.** There is no self-verification loop in v1: do the
    extraction once, carefully, present the table plus the Case A/B flag
    lists, and let the human review. Do not silently re-run or "fix" your own
    numbers against expectations — if something looks wrong, flag it.

## Domain-knowledge cross-checks

20. **A generic block/layer name is not proof of its billed identity — verify
    via the keynote/detail reference, not the name alone.** A "curb cut"
    block name is a physical description of the cut, not automatically a
    specific billed line item — on Woodman, "Standard Curb Cut" blocks
    turned out to be storm-drainage scuppers (curb openings that let street
    runoff into a swale), not driveway approaches, even though the name
    alone plausibly suggested either. What actually resolved it, and
    generalizes to similar naming ambiguity:
    - **Read the referenced detail sheet in its keynote/MULTILEADER callout
      text** (`keynote_legend_sections()` / `read_multileader_contents()`,
      rule 9). Woodman's callout was literally "CURB CUT PER DETAIL SD-003"
      — an `SD-` (storm drainage) detail prefix, not `ST-` (site/street),
      which settled it. A block/layer name alone is one signal (rule 3);
      the detail it cites is often a stronger one.
    - **Cross-check physical plausibility, not just the label.** A real
      driveway needs at least 8'-10' of width for a vehicle — measure the
      actual cut/block geometry rather than trusting the name. Also compare
      a candidate count against an independent count that should roughly
      bound it (e.g. lot count for anything meant to be one-per-house) — 31
      candidates against 23 lots is a signal worth investigating, not
      dismissing.
    - **Do not assume a resolved mapping transfers to another job at a fixed
      count or ratio.** Mountainside's own completed sheet has
      `SCUPPERS = 2`, a different order of magnitude from Woodman's 31 —
      the evidence-reading technique above generalizes, the specific number
      never does (rule 15).
