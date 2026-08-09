# Datastore redesign — immutable core, append-only events

**Status: PROPOSAL — not implemented, not scheduled.** Drafted 2026-08-09.
**Mandate:** same as the greenfield schema — straightforward code first, no backward
compatibility, one fresh-start cutover release. Run folders are the one exempt
immutable data: the cutover **relocates and converts container files; it never
rewrites measurement content** (`dataset.nc`, `parameters.json`, `result.json`,
scqat artifacts are untouched bytes).
**Scope note:** the campaign-writeback feature currently on `main` (unreleased:
campaign-level suggestions, `scqo accept --campaign`, index schema v10) keeps its
entire user surface, but its persistence **changes a little** under this design —
see §7. If this proposal is adopted before that feature ships, cut them together.

When this document conflicts with memory of the conversation that produced it,
this document wins.

---

## 1. What changes, what stays

The current skeleton — **run folders are the truth, `index.sqlite` is a disposable
projection** — is correct and stays. This redesign changes three things only:

| Axis | Today | Proposed |
|---|---|---|
| Mutability | truth files partially REWRITTEN in place (`record.json` tags/suggestions/decisions; `campaign.json` per repeat + post-finalize decisions) | truth files are **write-once or append-only**; all post-hoc activity is an appended EVENT; current state is a fold, computed by the index |
| Folder grammar | `<device>/<date>/<run_id>/`, campaigns a flat `<device>/campaigns/` sibling; runs of one cooldown scattered across shared date folders | `<device>/<cooldown>/runs/<date>/<short>/` and `<device>/<cooldown>/campaigns/<id>/` — a cooldown becomes one self-contained folder |
| Index schema | JSON columns scanned with `json_each`/`json_extract` for tags and trends | junction + value tables (free change: the index is a projection and never migrates — it reindexes) |

Explicitly unchanged: one `data_root` + ONE index for all samples; SQLite (WAL,
local disk) as the cache engine; netcdf per run (never zarr — directory-per-array
multiplies inodes and wrecks robocopy); the five-file run folder split; per-context
`<cooldown>/<setup>/scqo/` state + `.history.jsonl` sidecars (they already follow
the pattern this design generalizes); every `Session`/CLI/viewer surface
(`find_runs`, `load_run`, `tag_run`, `accept`, `suggest`, `accept_campaign`, …).

## 2. The problem, precisely

Four weaknesses, each currently papered over by a mechanism with its own cost:

1. **`record.json` is 95% immutable provenance, 5% mutable annotation.** That 5%
   (retro tags, notes, operator suggestions, accept/reject decisions) buys: the
   run-record lock-file discipline, the documented straddling-main hazard (an older
   scqo rewriting a record silently drops fields it doesn't know, e.g. the
   `[operator: …]` origin marker), and the INSTALL §5 merge-authority caveat
   (robocopy clobbers whichever side didn't win).
2. **`campaign.json` is worse.** While a campaign runs it is a LOCKLESS whole-file
   rewrite owned by the running process — anything another writer stores is
   *silently erased on the next repeat* (documented in
   `DataStore.edit_campaign_suggestions`), which forces the "after finalize this
   method is the only legal writer" rule and `suggest_campaign`'s refusal of a
   still-running campaign. Correct, but the correctness lives in comments.
3. **A cooldown's runs are scattered.** Context (backend_config, scqo state,
   physics) lives under `<device>/<cd>/<setup>/`, but the cycle's runs interleave
   with other cycles' runs in shared date folders. Archiving, retention ("drop raw
   `dataset.nc` older than two cycles, keep records"), or shipping a cycle to
   another server is a query, not a folder operation.
4. **Path length + index hot paths.** The device name is counted twice in every
   run path (folder + inside run_id) and the experiment name twice (run_id +
   artifact filename) — ~170 chars worst case today, fine but tight against
   Windows' 260 on deep copies. Unscoped tag/qubit filters scan JSON lab-wide, and
   `/trends` does `json_extract(fit, …)` per row.

## 3. The one rule

> **Nothing under `<data_root>` is ever rewritten after its first complete write.**
> A truth file is either write-once (sealed by its completion marker) or
> append-only (`.jsonl`). Current state = fold(write-once base, events in time
> order) — computed by the index, recomputable by `reindex`.

Consequences, all by construction rather than by policy:

- Multi-server aggregation is a plain `robocopy /E` — per-site event files (§6)
  never collide, so the "agree where tags are edited" caveat retires.
- Version-straddling is safe: an old reader IGNORES event types it doesn't know
  instead of rewriting a record without them. (Old *writers* are fenced naturally:
  their globs don't match the new layout.)
- Every decision has native history — who accepted what, when, from which machine —
  where today an accept overwrites the previous state.

## 4. Folder grammar

```
<data_root>/
  devices.toml                     # hand-edited registry (unchanged)
  index.sqlite*                    # the projection (unchanged role, new schema)
  <device>/
    components.toml  design.toml  cooldowns.toml        (unchanged)
    <cooldown>/
      <setup>/backend_config/     <setup>/scqo/          (unchanged)
      runs/<YYYY-MM-DD>/<stamp>-NN/                      # run folders (§5)
      campaigns/<stamp>-<label>-NN/                      # campaign folders (§7)
    _nocycle/runs/<YYYY-MM-DD>/…   # library Sessions with no cooldown registry
```

- The literal `runs/` level is what makes setup folders, `campaigns/` and date
  folders collision-free under one cooldown. `runs` and `campaigns` become
  **reserved setup names**, refused loudly by the cooldowns.toml loader.
- `_nocycle` is a pseudo-cooldown (reserved cooldown id), so ONE glob shape covers
  both: runs `*/*/runs/*/*/record.json`, campaigns `*/*/campaigns/*/plan.json`.
- **Folder name ≠ identity.** `run_id` keeps today's form
  (`<stamp>-<device>-<experiment>-<seq>` — its global uniqueness by construction
  and sortability are load-bearing); the FOLDER shrinks to `<stamp>-NN` (23 chars,
  exclusive-mkdir still the collision guard, ms stamp still the human sort key).
  Identity authority is `record.json`, which is already how `reindex` works today.
  Campaign folders KEEP the label (`<stamp>-<label>-NN`): they are few, shallow,
  and hand-browsed, so the label earns its chars; the device name drops from both
  folder names (it is two levels up) while staying inside the ids.
- Path math, worst realistic case (`D:\qpu_data`, device 18 chars, longest
  experiment 34): today ≈ 170 chars → proposed ≈ 130. The cooldown level costs 4;
  the folder rename saves ~45.

## 5. The run folder

```
<device>/<cd>/runs/<YYYY-MM-DD>/<stamp>-NN/
  dataset.nc  parameters.json  result.json          # write-once (bytes, untouched by anything)
  device_before.json  device_after.json             # write-once
  record.json                                       # completion marker AND freeze marker
  <target>/…                                        # scqat artifacts, write-once
  events/<site>.jsonl                               # append-only; absent = no post-hoc activity
```

`record.json` keeps everything true AT the run: run-time tags (`run(…, tags=…)`,
config `default_tags`), the estimator-born suggestions (status `"pending"` at
birth), operator, cooldown/setup stamps, campaign/repeat/step columns. Its write
stays the completion marker — and now also marks the folder immutable. Everything
that happens LATER is an event.

## 6. Events

One vocabulary for runs and campaigns. A line is one JSON object:

```
{"t": "<ISO-8601 local, with offset>", "site": "<hostname>", "by": "<operator>", "type": "…", …payload}
```

| type | payload | fold rule |
|---|---|---|
| `tag_add` / `tag_remove` | `tag` | set fold in time order (birth tags from record.json/plan.json are the base) |
| `note_set` | `text` | last-writer-wins |
| `suggestion_add` | full suggestion, `origin:"operator"` | appends to the base list; id assigned below |
| `decision` | `ids`, `action: accept\|reject`, per-field applied/skipped detail, era info, `updated_device` | last decision per id wins (re-accept/rollback = another event; full history retained) |
| `proposed` | campaign only, §7: the aggregate suggestion list + `suggestion_problems` | replaces-nothing; base list for campaign decisions |
| `finalized` | campaign only, §7: `status`, `ended_at`, repeat counts | terminal marker; absence = running-or-interrupted |

- **Suggestion ids are stable and mint-once**: `s0…sN` for record.json birth
  suggestions (list order), `c0…cN` for a campaign's proposed aggregate,
  `<site>:<n>` for operator adds. Decisions reference ids, never list indices.
- **Per-site files are the concurrency design**: cross-host writers never share a
  file (site = hostname), so file sync unions them; same-host writers append under
  the existing `_file_lock` discipline on their site file. Torn trailing lines are
  tolerated exactly as `repeats.jsonl` and the history sidecars already do.
- Fold order is `(t, site, line#)`. Wall clocks across sites are assumed sane
  (lab PCs on NTP); a skewed clock can misorder two decisions on one id — the same
  exposure today's "last rewrite wins" has, now at least visible in the history.
- The appending process also folds and UPDATEs the affected index row (as
  `tag_run` does today); a skipped index write heals on `reindex`.

## 7. Campaigns — changed a little

The user surface of the campaign feature — including the unreleased writeback
(`update='suggest'` refused multi-repeat; the aggregate proposed ONCE at finish;
`scqo accept --campaign <id>`; campaigns as first-class provenance sources) — is
**unchanged**. What changes is persistence:

```
<device>/<cd>/campaigns/<stamp>-<label>-NN/
  plan.json          # WRITE-ONCE at start: campaign_id, device, cooldown/setup stamps,
                     #   plan + cadence + stop conditions, started_at
  repeats.jsonl      # append-only per-repeat skeleton — UNCHANGED (run_ids/outcome/timing,
                     #   never fit values)
  events/<site>.jsonl  # proposed / finalized / decision / tag / note events
  statistics.png     # best-effort finalize artifact (regenerable; unchanged)
```

Deltas from today's `campaign.json`, one by one:

- **`campaign.json` is retired.** The per-repeat whole-file rewrite disappears;
  with it goes the silently-erased-concurrent-edit hazard and the "only legal
  writer after finalize" rule — there is no rewrite for an edit to be erased BY.
  `suggest_campaign` no longer needs to refuse a running campaign for safety
  (whether it should remain refused for semantics — deciding against a moving
  aggregate — is a separate choice; keep the refusal).
- **Status is derived, not stored**: a `finalized` event present → its status
  (`done`/`stopped`/`failed`); absent → running-or-interrupted, disambiguated
  exactly as today by activity (an absent `ended_at` already meant "unfinished").
  The finalize append happens in the same `finally` as today's manifest rewrite —
  every exit path, and an append is strictly MORE robust than a whole-JSON rewrite
  (smaller write, torn-line tolerant instead of unparseable-file fatal).
- **Statistics become pure projection.** They are already documented as
  rebuildable from the children; the runner keeps mid-campaign visibility by
  upserting the index row per repeat (it already touches the index per child run).
  `check_campaign()` reads the projection; partial-repeat semantics (children
  kept, `repeats_partial`, never `repeat_done`) are untouched — they live in
  `repeats.jsonl` + the children, both append-only already.
- **The finish-time aggregate suggestions become a `proposed` event**; decisions
  on them are `decision` events with `c<n>` ids. Provenance credit
  ("campaign as a first-class value source") reads the same information from the
  fold instead of from a rewritten manifest.
- **Location moves under the cooldown.** A campaign cannot span cycles anyway
  (`scqo device cooldown end` refuses subsequent runs), so the container finally
  matches the invariant. The not-under-a-day-folder rationale (overnight crossing
  midnight) is preserved — `campaigns/` stays a date-less sibling of `runs/`.

## 8. The index — free to be good

Because the index is a projection, its schema changes cost nothing (no migration
path, ever — delete + `reindex` IS the upgrade). With the truth layer settled,
normalize the hot paths:

- `runs_tags(run_id, tag)` and `runs_targets(run_id, target)` junction tables
  replace the `json_each` scans (the one documented scale limitation).
- `fit_values(run_id, target, quantity, value REAL)` replaces `/trends`'
  per-row `json_extract` — trends become an indexed range read.
- `suggestions_pending` (runs and campaigns) is computed by the event fold.
- `reindex` = scan write-once files, fold events, rebuild all tables — same
  one-transaction discipline as today.

## 9. Multi-server aggregation

`robocopy <src> <dst> /E /XF index.sqlite*` and `reindex` — now with **no
authority caveat**: annotations from every site coexist as distinct
`events/<site>.jsonl` files and fold deterministically. The INSTALL §5 merge
note's "agree where runs are tagged" paragraph retires. Everything else there
(`/E` not `/MIR`, names are the merge keys, `devices.toml` by hand) stands.

## 10. Cutover (one shot, per the no-compat policy)

1. **Mover script** (offline, per data_root): relocate each run folder to
   `<device>/<cooldown>/runs/<date>/` using the cooldown stamp in its own
   `record.json` (empty stamp → `_nocycle/`). Folders are MOVED, not renamed —
   grandfathered long names are legal forever (folder ≠ identity); only new runs
   get short names. Measurement bytes untouched.
2. **Campaign conversion** (lossless, mechanical): split each `campaign.json`
   into `plan.json` + synthesized events (`proposed` from its suggestion list,
   `decision` events for already-decided entries, `finalized` from
   status/ended_at), relocate under the cooldown. This converts a container
   format once; it is not a compat shim.
3. Retro-mutation history that predates the cutover (who tagged what, when) was
   never stored and cannot be synthesized — pre-cutover records fold as birth
   state. Accepted.
4. Delete `index.sqlite*`, `reindex`. Upgrade every machine in the combo release
   (standard policy); old code cannot mis-write the new layout — its globs and
   paths simply don't match.

## 11. Open questions

- `site` = hostname: sufficient in this lab (distinct PC names); a rename mid-life
  splits nothing (files fold together regardless of name), so low stakes.
- Should `_nocycle` runs be refused on lab servers (where a registry always
  exists) and allowed only for library/test Sessions? Leaning yes.
- Event-file growth: negligible (tens of lines per run worst case), so no
  compaction story is planned — flag if a use appears that appends per shot.
- Analysis laptops could point DuckDB read-only at the NAS mirror's JSON/JSONL
  directly (network-safe where SQLite WAL is not) — additive, out of scope here.
