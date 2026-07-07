# Cutting a release (manager checklist)

A release is a **combo**: one tag name across the scqo-versioned repos plus a pinned
scqat tag, recorded in [RELEASES.toml](RELEASES.toml). Born from the v0.4.0 lesson:
tagging only SCQO left the server's checkout-by-tag procedure unable to bring the
drivers forward, and nobody could tell which repo states belonged together.

1. **CI green** on SCQO main (3 OS). Driver + contrib test suites green in their venvs.
2. **Version metadata matches the tag**: bump `version` in SCQO's `pyproject.toml` to
   the release number (the `scqo --version` a user sees). scqat manages its own
   version line with the same rule (its release checklist).
3. **Tag the four scqo-versioned repos with the SAME name** — SCQO, LCHQBDriver,
   LCHQMDriver, scqo-contrib — at their release commits (unchanged repos get a
   no-change re-tag; annotate it as such):

   ```powershell
   git tag vX.Y.Z -m "vX.Y.Z: <one-liner>"; git push origin vX.Y.Z
   ```

4. **Record the combo in RELEASES.toml**: all four tags + the scqat pin + a notes line
   that names any REQUIRED upgrade action (e.g. v0.4.0's editable reinstalls).
5. **Push everything**; verify with `git ls-remote --tags origin` per repo.
6. **Server upgrade** = INSTALL §5: `git fetch --tags && git checkout vX.Y.Z` in the
   four repos (+ scqat at its pinned tag), re-run the §1 `uv pip install -e` lines
   when the notes say so, restart the viewer, then `scqo doctor` on a student account.

Never move a pushed tag. A fix after tagging = a new patch release (this is how
v0.4.1 exists).
