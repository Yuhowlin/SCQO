# RELEASES.d — the pending-release ledger (one fragment file per feature)

Every release-worthy feature landed on `main` gets **one TOML fragment file here**,
written by whoever lands it, consumed (and deleted) by whoever cuts the next combo
release ([RELEASING.md](../RELEASING.md) step 0). One file per feature means
concurrent agents never edit a shared ledger file — no merge conflicts by
construction.

## Fragment format — `<feature-slug>.toml`

```toml
name = "fit-tau-seeds"            # = the filename stem
repos = { scqo = "12a82a9", scqat = "42aafd8" }   # LAST commit per repo
# keys: scqo / scqat / lchqmdriver / lchqbdriver / scqo-contrib
kind = "additive"                 # or "breaking"
coupling = "scqo needs scqat >= 42aafd8: ..."     # lockstep floors, esp.
                                  # SILENT-failure ones ("" if none)
validated = "hardware 5Q4C q1 2026-08-06"         # or "offline" / "unverified"
notes = "One release-facing sentence; use UPGRADE REQUIRES / BEHAVIOR CHANGE phrasing."
```

Write the fragment as the feature's **LAST step**, after every repo's commits have
landed — the ledger must only ever list COMPLETE features, so a release cannot catch
a feature half-landed across repos. If your slug already exists, pick another.

## Multi-agent rules for the shared D:\github trees

Several agents work these trees simultaneously, and the trees are LIVE — the uv
venvs are editable installs of them and hardware sessions import them. Hence:

1. **One agent = one feature = its own file set across repos.** Before editing any
   file in any repo, check that repo's `git status` — a file already modified by
   someone else is OFF LIMITS (never edit, stage, revert, or commit it). Need an
   off-limits file → rule 5.
2. **Ledger**: write your own fragment here; never edit a shared ledger file. The
   fragment's `repos` table is the cross-repo binder for the feature.
3. **Commit by explicit pathspec, one repo at a time**:
   `git -C <repo> add <your files>` then `git -C <repo> commit -m "..." -- <your files>`
   — the pathspec confines the commit, so another agent's concurrently staged files
   cannot ride along (bare `git commit` after `git add` is the sweep hazard).
   Verify with `git show --stat HEAD`. Never `git add -A` / `commit -a`; never
   `reset`/`restore`/`checkout` paths you don't own. Land commits in dependency
   order (scqat → SCQO → drivers) so the trees stay import-consistent throughout.
4. **Never switch branches in the shared trees** — `main` stays checked out
   (editable installs + possibly-running hardware sessions read the trees live).
5. **Same-file conflict or risky refactor → a `git worktree` SET, not a branch
   switch**: for each repo the feature touches,
   `git worktree add <path outside the repo> -b feature/<slug>` — isolated
   checkouts of one branch name per repo while `main` stays put for everyone else.
   Merge back in dependency order (scqat first, then SCQO, then drivers), delete
   the worktrees. Caveats: a worktree is NOT what the venvs import — per-repo
   offline tests need a path override (`sys.path.insert` / `uv run --project`);
   cross-repo integration tests (glue suites, `scqo run`) and hardware validation
   happen only on `main` after the merges.

## At release time (the consuming agent)

Read every `*.toml` here → cross-check `git log <last-tag>..HEAD` per repo (a
fragment may be missing — flag it) → pick versions (any `kind = "breaking"` drives
the bump; `coupling` sets the scqat floor) → write the `[vX-Y-Z]` block in
[RELEASES.toml](../RELEASES.toml) from the fragments → `git rm` the consumed
fragments in the same commit → then follow RELEASING.md steps 1–6 (pyproject bumps
are PART of the release; tags; editable re-pins).
