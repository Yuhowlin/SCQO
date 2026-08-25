# Cutting a release (manager checklist)

A release is a **combo**: one tag name across the scqo-versioned repos plus a pinned
scqat tag, recorded in [RELEASES.toml](RELEASES.toml). Born from the v0.4.0 lesson:
tagging only SCQO left the server's checkout-by-tag procedure unable to bring the
drivers forward, and nobody could tell which repo states belonged together.

## The version rule (0.x)

One combo number covers the scqo-versioned repos; scqat versions independently on
its own line under the SAME rule, tied to the combo by the coupling floors.
**Bump = max severity over the consumed fragments** (each fragment's `kind`):

- As of **v1.0.0** (declared deliberately 2026-08-09 with the v1 driver
  restructure — this line is the declaration) the full SemVer rule applies:
  `breaking` → **x+1**, `additive` → **y+1**, `fix` → **z+1**. The 0.x rule
  (breaking/additive → y+1, all-fix → z+1) is history.
- **Breaking** is judged against OUR consumers, not just the Python API: an
  existing user artifact (script, campaign plan, parameters.toml,
  config.toml/user.toml, stored state) must change to keep working. That includes
  renamed/removed registered experiment names or Parameters fields, renamed
  catalog fields/knobs, state-file `schema` number bumps, config key changes, and
  contract/dataset changes that break re-analysis of old runs.
- **Fix** = behavior-preserving repair or cosmetic change (figure labels,
  annotations, docs). Repairing a BROKEN fit is still `fix` even though numbers
  change — its notes must say re-analysis refreshes old artifacts (run folders
  stay immutable). Changing a WORKING estimator's numbers is a behavior change =
  `additive`.
- **Breaking ⇒ the RELEASES.toml notes line carries the upgrade action**,
  aggregated from the fragments' UPGRADE REQUIRES phrasing (v0.24.0's notes are
  the model). No shims or aliases ship (house rule) — the notes ARE the migration
  path.

0. **Read the pending-feature ledger `RELEASES.d/`** — ONE fragment file per
   feature (format + the multi-agent rules in `RELEASES.d/README.md`): per-repo
   commits, breaking/additive kind, lockstep couplings (the scqat floor comes from
   these) and validation status. Cross-check against `git log <last-tag>..HEAD` per
   repo (a fragment may be missing — the ledger is maintained by whoever lands each
   feature). Build the new `[vX-Y-Z]` block FROM the fragments, then `git rm` the
   consumed fragments in the same commit.

1. **CI green** on SCQO main (3 OS). Driver + contrib test suites green in their venvs.
2. **Version metadata matches the tag**: bump `version` in SCQO's `pyproject.toml` to
   the release number (the `scqo --version` a user sees). scqat manages its own
   version line with the same rule (its release checklist).
3. **Tag the three scqo-versioned repos with the SAME name** — SCQO, scqo-qblox,
   scqo-qm — at their release commits (unchanged repos get a no-change re-tag;
   annotate it as such). `scqo-contrib` is **retired** as of v3.2.0 and is no longer
   tagged; `RELEASES.toml` keeps its pin line for history only:

   ```powershell
   git tag vX.Y.Z -m "vX.Y.Z: <one-liner>"; git push origin vX.Y.Z
   ```

4. **Record the combo in RELEASES.toml**: all three tags + the scqat pin + a notes line
   that names any REQUIRED upgrade action (e.g. v0.4.0's editable reinstalls).
5. **Push everything**; verify with `git ls-remote --tags origin` per repo.
6. **Server upgrade** = INSTALL §5: `git fetch --tags && git checkout vX.Y.Z` in the
   three repos (+ scqat at its pinned tag), re-run the §1 `uv pip install -e` lines
   when the notes say so, restart the viewer, then `scqo doctor` on a student account.

Never move a pushed tag. A fix after tagging = a new patch release (this is how
v0.4.1 exists).
