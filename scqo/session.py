"""Session — the single entry point used identically by humans and AI agents.

Greenfield port of the state half of :mod:`scqo.session` (the run()/catalog()
experiment flow plugs in with the experiment-base cutover). Everything
crosses this boundary as plain JSON-able Python, so the same calls drive a
manual notebook and an LLM tool-use loop::

    sess.set_values({"q0.pi_amp": 0.2})   # runless manual write, validated,
                                          #   recorded as (manual)
    sess.suggest(run_id, {"q0.readout_freq_hz": 5.91e9})  # attach a
                                          #   figure-read value to a run
    sess.accept(run_id)                   # apply pending suggestions
    sess.reject(run_id, comment="...")    # decline them (metadata only)
    sess.device_state()                   # per-entity operating state
    sess.qubit_state("q0")                # the per-qubit assembled view
    sess.physical_state()                 # the sample's measured physics
    sess.history()                        # every recorded change

Addressing is the QUBIT-CLOSURE sugar (:meth:`Roster.resolve_field`):
``q0.pi_amp`` routes to ``q0_xy``, ``q0.readout_freq_hz`` to ``q0_ro``,
``q0.f_r_hz`` to ``q0_res`` — explicit entity names always work. Suggestions
carry the field's ROLE; applying routes facts to the physical store and
knobs/monitors through the recording device (vendor-push-first).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .datastore import DataStore
from .design import Design
from .device import CompositeView, RecordingDevice
from .roster import Roster
from .stores import Store, _current_operator, _now, physical_store, state_store
from .suggestions import (
    Suggestion,
    decision_editor,
    load_suggestions,
    pending_count,
    reject_campaign_suggestions,
    reject_suggestions,
    select_suggestions,
)

if TYPE_CHECKING:  # lazy at runtime: campaign.py is only needed by run_campaign
    from .campaign import CampaignPlan


def _safe_progress(on_progress):
    """Wrap a progress callback so it can never kill the campaign.

    A progress display is a convenience; an overnight measurement is not. A
    raising callback is reported ONCE on stderr and then dropped for the rest of
    the run — the same doctrine as ``_scqat``'s artifact-write fallback, where
    losing the pretty output must never lose the measurement. ``None`` becomes a
    no-op so the call sites stay unconditional."""
    if on_progress is None:
        return lambda event: None
    state = {"broken": False}

    def emit(event: dict):
        if state["broken"]:
            return None
        try:
            return on_progress(event)
        except Exception as err:  # pragma: no cover - defensive
            import sys

            state["broken"] = True
            print(f"scqo: progress callback failed ({type(err).__name__}: {err}); "
                  "continuing without progress output", file=sys.stderr)
            return None

    return emit


def _describe_error(err: BaseException) -> str:
    """The run-record error line: the exception plus its cause chain.

    ``f"{type(err).__name__}: {err}"`` alone loses the diagnostic whenever a
    library re-raises bare — qualang_tools' qm_session does ``raise Exception
    from e`` around a QM open failure, reducing the saved record to
    ``"Exception: "`` while the real reason (e.g. a PHYSICAL CONFIG ERROR /
    DEADLINE_EXCEEDED from a wedged gateway) survives only in the vendor log.
    Walking ``__cause__``/``__context__`` (the same preference order as
    Python's traceback: an explicit cause wins, an implicit context is
    dropped after ``from None``) keeps that reason in the JSON result. The
    chain is joined on ONE line because a campaign's progress display shows
    only an error's first line."""
    parts: list[str] = []
    seen: set[int] = set()
    node: BaseException | None = err
    while node is not None and id(node) not in seen and len(parts) < 10:
        seen.add(id(node))
        text = str(node).strip()
        parts.append(f"{type(node).__name__}: {text}" if text
                     else type(node).__name__)
        if node.__cause__ is not None:
            node = node.__cause__
        elif not node.__suppress_context__:
            node = node.__context__
        else:
            node = None
    return "; caused by ".join(parts)


def _repeat_skeleton(row: dict) -> dict:
    """One repeat, without the fit VALUES — what ``repeats.jsonl`` stores.

    The child's ``result.json`` (and the indexed ``runs.fit`` column) is the one
    truth for every number; a campaign's only derived numbers are the aggregate
    statistics in the manifest. Copying fits into a second file would create a
    second authority that can silently disagree with the first."""
    return {
        **{k: v for k, v in row.items() if k != "steps"},
        "steps": [{k: v for k, v in step.items() if k != "fit"} for step in row["steps"]],
    }


class Session:
    """Bind a backend and expose the state-management API over an SCQO-owned
    config (the experiment flow arrives with the experiment cutover)."""

    def __init__(
        self,
        backend,
        roster: Roster,
        *,
        design: Design | None = None,
        scqo_dir: str | Path | None = None,
        data_root: str | Path | None = None,
        device_name: str = "device",
        state_sync: Literal["push", "pull"] = "pull",
        default_tags: list[str] | None = None,
        parameter_defaults: dict[str, dict[str, Any]] | None = None,
        parameter_defaults_source: str | None = None,
        backend_label: str | None = None,
        setup_name: str | None = None,
        cooldown_id: str | None = None,
    ) -> None:
        self.backend = backend
        self.roster = roster
        self.design = design if design is not None else Design({})
        self.backend_label = backend_label or type(backend).__name__
        self.setup_name = setup_name or ""
        self.cooldown_id = cooldown_id or ""
        #: the per-(cooldown, setup) scqo/ folder holding both stores
        #: (None = in-memory session: full validation, no persistence).
        self.scqo_dir = Path(scqo_dir) if scqo_dir is not None else None
        self._persist = scqo_dir is not None
        self.state = state_store(scqo_dir, roster, setup=self.setup_name)
        if scqo_dir is None and data_root is not None:
            # Setup-less direct-API escape hatch: measured physics still
            # persists at the device level — losing it on restart is THE
            # regression the store prevents (old-session parity).
            from .stores import PHYSICAL_FILE, Store
            self.physical = Store(
                Path(data_root) / device_name / PHYSICAL_FILE, roster,
                roles=frozenset({"fact"}), setup=self.setup_name)
        else:
            self.physical = physical_store(scqo_dir, roster,
                                           setup=self.setup_name)
        self.device = RecordingDevice(backend.device, roster, self.state,
                                      on_load=state_sync)
        self.datastore = (
            DataStore(data_root, device_name=device_name,
                      setup=self.setup_name or None,
                      cooldown=self.cooldown_id or None)
            if data_root is not None else None)
        self.default_tags = list(default_tags or [])
        self.parameter_defaults = dict(parameter_defaults or {})
        self.parameter_defaults_source = parameter_defaults_source

    # ----------------------------------------------------------- run flow

    def catalog(self) -> list[dict]:
        """Available measurements with their JSON parameter schemas; standing
        parameter defaults overlay the schemas with x-default-source."""
        import copy

        from . import experiments as registry
        entries = registry.catalog()
        if not self.parameter_defaults:
            return entries
        source = self.parameter_defaults_source or "parameter_defaults"
        overlaid = []
        for entry in entries:
            table = self.parameter_defaults.get(entry["name"]) or {}
            props = entry["parameters_schema"].get("properties", {})
            known = {k: v for k, v in table.items() if k in props}
            if known:
                entry = copy.deepcopy(entry)
                schema = entry["parameters_schema"]
                required = schema.get("required", [])
                for key, value in known.items():
                    schema["properties"][key]["default"] = value
                    schema["properties"][key]["x-default-source"] = source
                    if key in required:
                        required.remove(key)
            overlaid.append(entry)
        return overlaid

    def run(self, experiment: str, params: dict[str, Any],
            update: str | bool = "suggest", *,
            tags: list[str] | None = None, note: str = "",
            skip_artifacts: bool = False,
            campaign: tuple[str, int, int] | None = None) -> dict:
        """Run an experiment by name; return its structured result as a dict.
        ``update``: "suggest" (default — update() captured as pending
        Suggestions), "apply" (capture then immediately accept), "none".
        A failed run returns a structured result with ``error`` set, never
        raises; with a data_root every run persists to its own folder.

        ``skip_artifacts`` writes no ``analysis/`` folder — no figures, and no
        scqat metadata or plotdata either (they share one switch); ``dataset.nc``
        and ``result.json`` still land. It exists for long campaigns, where
        per-qubit artifacts multiply by the repeat count.

        ``campaign`` is ``(campaign_id, repeat_idx, step_idx)``, set only by
        :meth:`run_campaign` — it stamps the run record so a child is
        self-describing."""
        from pydantic import ValidationError

        from . import experiments as registry
        from .suggestions import SuggestionCapture

        mode = {True: "apply", False: "none"}.get(update, update)
        if mode not in ("suggest", "apply", "none"):
            raise ValueError(
                f"update must be 'suggest', 'apply' or 'none' (or a bool), "
                f"got {update!r}")
        cls = registry.get(experiment)
        defaults = self.parameter_defaults.get(experiment, {})
        merged = {**defaults, **params}
        try:
            validated = cls.Parameters(**merged)
        except ValidationError as err:
            # "suggestions" unconditionally: every return of run() carries the
            # key, so a caller in a loop (run_campaign) can read it blind.
            return {**self._invalid_params(cls, merged, defaults, params, err)
                    .model_dump(mode="json"), "suggestions": []}
        exp = cls(self.backend, validated)
        exp.device = self.device  # route through the recording surface
        exp.design = self.design

        gate_error = self._validate_targets(cls, exp)
        if gate_error is not None:
            return {**gate_error.model_dump(mode="json"), "suggestions": []}

        started_at = _now()
        run_id = run_dir = None
        if self.datastore is not None:
            run_id, run_dir = self.datastore.new_run_dir(experiment)
            if not skip_artifacts:
                exp.artifact_dir = run_dir / "analysis"

        device_before = self.device.snapshot()
        self.device.set_context(experiment, run_id)
        updated = False
        suggestions: list[Suggestion] = []
        try:
            try:
                result = exp.run()
            except Exception as err:
                result = self._failure(cls, exp, err)
            else:
                if mode != "none" and result.any_success:
                    capture = SuggestionCapture(self.device, self.physical,
                                                self.roster)
                    exp.device = capture
                    try:
                        exp.update()
                        suggestions = capture.suggestions
                    except Exception as err:
                        result.error = (
                            f"measurement succeeded but suggestion capture "
                            f"failed (nothing suggested or applied): "
                            f"{type(err).__name__}: {err}")
                    finally:
                        exp.device = self.device
                    if mode == "apply" and suggestions:
                        try:
                            applied, errors = self._apply(
                                suggestions, experiment=experiment,
                                run_id=run_id)
                            updated = bool(applied)
                            if self._persist:
                                self.device.save()
                            self.physical.save()
                            if errors:
                                result.error = (
                                    f"measurement succeeded but device "
                                    f"update failed (device may be "
                                    f"partially updated): "
                                    + "; ".join(errors))
                        except Exception as err:
                            result.error = (
                                f"measurement succeeded but device "
                                f"update/save failed (device may be "
                                f"partially updated): "
                                f"{type(err).__name__}: {err}")
        finally:
            self.device.set_context(None, None)

        payload = result.model_dump(mode="json")
        suggestion_dicts = [s.model_dump(mode="json") for s in suggestions]
        if self.datastore is not None:
            try:
                power_context = getattr(self.backend, "power_context",
                                        lambda t: {})(
                    list(getattr(exp.params, "targets", []))) or {}
            except Exception:
                power_context = {}
            try:
                self.datastore.persist_run(
                    run_id=run_id, run_dir=run_dir, experiment=experiment,
                    params=exp.params, dataset=exp.dataset, result=payload,
                    device_before=device_before,
                    device_after=self.device.snapshot(),
                    started_at=started_at, ended_at=_now(),
                    backend=self.backend_label, updated_device=updated,
                    suggestions=suggestion_dicts,
                    power_context=power_context,
                    tags=list(dict.fromkeys(
                        [*self.default_tags, *(tags or []),
                         *getattr(exp, "seed_tags", [])])),
                    note=note,
                    campaign=campaign)
            except Exception as err:  # never lose a measurement over a save
                payload["datastore_error"] = f"{type(err).__name__}: {err}"
            else:
                payload["run_id"] = run_id
                payload["data_path"] = str(run_dir)
        payload["suggestions"] = suggestion_dicts
        return payload

    # ----------------------------------------------------------- campaigns

    def run_campaign(
        self,
        plan: "CampaignPlan | dict",
        *,
        update: str | bool = "none",
        tags: list[str] | None = None,
        note: str = "",
        on_progress=None,
    ) -> dict:
        """Walk a :class:`~scqo.campaign.CampaignPlan`'s steps ``repeat`` times.

        OUTER repetition: every (repeat, step) is a full :meth:`run` with its own
        run folder, dataset, fit and TIMESTAMP, stamped with
        ``(campaign, repeat_idx, step_idx)``. Interleaved by construction — the
        outer loop is the repeat — so every quantity within one repeat shares a
        drift epoch and ``T1[k]`` is comparable with ``T2*[k]``. Returns the
        manifest: status, counts, the per-(experiment, target, quantity)
        statistics, and the repeat skeleton.

        ``update`` defaults to ``"none"``, inverting :meth:`run`'s default
        deliberately. Each child would store its OWN pending suggestion set, all
        capturing the same ``before``; accepting repeat 0 moves the store, so
        repeats 1..N-1 would every one report ``stale`` — N sets of undecidable
        proposals. ``"suggest"`` is therefore REFUSED for more than one repeat.
        The writeback happens ONCE instead, at finish: the statistics are
        replayed through each experiment's own ``update()`` and stored as
        campaign-level PENDING suggestions on the manifest (policy:
        ``plan.writeback``; decide with :meth:`accept_campaign` /
        ``scqo accept --campaign``). ``"apply"`` is allowed with a warning:
        legitimate for a repeated RE-TUNING campaign, but it moves the device
        under its own measurement (so the statistic measures the feedback
        loop, not the qubit), each child's ``Store.save()`` rewrites the whole
        history sidecar — and no aggregate suggestions are generated (the
        series is a feedback trace, not repeated measurements of one value).

        PRE-FLIGHT: before repeat 0 every step's Parameters are constructed and
        the roster target gate is run — both pre-hardware, no side effects. A
        plan with a typo'd parameter or an off-roster target is REFUSED WHOLE,
        with a per-step problem list, instead of failing identically N times.
        Nothing is created on disk in that case.

        A repeat counts as FAILED only when NO step in it produced a success —
        the instrument-disconnected signature. One flaky estimator among four
        steps shows up as ``n_missing`` in the statistics, not as a failed
        repeat, so it cannot trip ``on_error`` or the consecutive-failure
        breaker on an otherwise healthy overnight run.

        Never raises once the plan is valid. The manifest is finalized in a
        ``finally``, so it is written on EVERY exit path — a normal stop, Ctrl-C
        anywhere (including the cadence sleep, where a campaign with ``period_s``
        spends most of its wall clock), or an unexpected error (``status="failed"``
        with the exception named). An interrupted repeat keeps whatever steps it
        already completed, marked ``partial`` and counted in ``repeats_partial``
        rather than in ``repeat_done``; nothing measured is dropped. The step that
        was actually running leaves a folder with no ``record.json``, which reindex
        already skips as an incomplete run.

        ``on_progress(event) -> str | None`` receives, in order per repeat:
        ``cadence_wait`` (only when the period actually sleeps), ``repeat_start``,
        one ``step_done`` per step, then ``repeat_done``. This method itself never
        prints — the library stays renderer-free and the CLI does the rendering
        (``scqo.cli._campaign.progress_lines``). A notebook can pass its own::

            sess.run_campaign(plan, on_progress=lambda e: print(e["kind"]))

        Returning ``"stop"`` ends the campaign, and is honoured **only** on
        ``repeat_done``: stopping mid-repeat would leave a half-walked bundle
        whose quantities no longer share a drift epoch, which is exactly the
        invariant the interleaved ordering exists to protect.
        """
        import time

        from .campaign import CampaignPlan, aggregate

        plan = plan if isinstance(plan, CampaignPlan) else CampaignPlan(**dict(plan))
        on_progress = _safe_progress(on_progress)
        mode = {True: "apply", False: "none"}.get(update, update)
        if mode not in ("suggest", "apply", "none"):
            raise ValueError(
                f"update must be 'suggest', 'apply' or 'none' (or a bool), "
                f"got {update!r}")
        if mode == "suggest" and plan.repeat != 1:
            raise ValueError(
                "update='suggest' is refused for a multi-repeat campaign: every "
                "repeat would capture the same `before`, so accepting one makes "
                "all the others stale and undecidable. Use update='none' (the "
                "default) — the aggregate is proposed ONCE at finish as "
                "campaign-level suggestions, decided with `scqo accept "
                "--campaign <id>` — or update='apply' if you really mean a "
                "repeated re-tuning.")

        started_at = _now()
        started_mono = time.monotonic()
        manifest: dict[str, Any] = {
            "schema": 1,
            "campaign_id": None,
            "label": plan.label,
            "device": self.datastore.device_name if self.datastore is not None else "",
            "backend": self.backend_label,
            "operator": _current_operator(),
            "cooldown": "",
            "setup": "",
            "started_at": started_at,
            "ended_at": None,
            "status": "running",
            "stop_reason": "",
            "update_mode": mode,
            "experiments": plan.experiments,
            "targets": plan.targets(),
            "repeat_planned": plan.repeat,
            "repeat_done": 0,  # WHOLE repeats; a partial one is counted separately
            "repeats_partial": 0,
            "repeats_failed": 0,
            "plan": plan.model_dump(mode="json"),
            "statistics": {},
            "tags": list(dict.fromkeys([*self.default_tags, *(tags or []), *plan.tags])),
            "note": note or plan.note,
            "path": None,
        }

        problems = self._preflight(plan)
        if problems:
            manifest["status"] = "failed"
            manifest["stop_reason"] = "the plan was refused before any hardware"
            manifest["ended_at"] = _now()
            manifest["error"] = "; ".join(problems)
            manifest["problems"] = problems
            manifest["repeats"] = []
            return manifest

        campaign_id = campaign_dir = None
        era: tuple[str, str] | None = None
        if self.datastore is not None:
            campaign_id, campaign_dir = self.datastore.new_campaign_dir(plan.label)
            era = self.datastore.run_stamps()
            manifest["campaign_id"] = campaign_id
            manifest["cooldown"], manifest["setup"] = era
            manifest["path"] = campaign_dir.relative_to(self.datastore.data_root).as_posix()
            self.datastore.persist_campaign(
                campaign_id=campaign_id, campaign_dir=campaign_dir, manifest=manifest)

        rows: list[dict] = []
        consecutive_failures = 0
        stop_reason = ""
        status = "complete"
        repeat_idx = 0
        row: dict | None = None  # the in-flight repeat, so an interrupt can keep it

        def _keep_partial(pending: dict | None) -> None:
            """Record a half-walked repeat instead of discarding what it measured.

            Its completed steps already ran, already persisted their own run folders
            and are already stamped with (campaign, repeat_idx, step_idx) — dropping
            them from the manifest would leave campaign_runs() returning more
            children than repeat_done accounts for, silently.

            Safe because a partial repeat is always the LAST one: aggregate()
            advances its per-experiment slot per (row, step) occurrence, so a short
            row would misalign anything AFTER it, and there is nothing after it. The
            steps that ran contribute their values; the rest contribute n_missing.
            """
            if not pending or not pending["steps"]:
                return
            try:
                pending["ended_at"] = _now()
                pending["duration_s"] = (time.monotonic() - started_mono
                                         - pending["elapsed_s"])
                pending["failed"] = not any(s["ok"] for s in pending["steps"])
                pending["partial"] = True
                rows.append(pending)
                manifest["repeats_partial"] = 1
                manifest["repeats_failed"] = sum(1 for r in rows if r["failed"])
                manifest["statistics"] = aggregate(rows)
                if campaign_dir is not None:
                    self.datastore.append_campaign_repeat(
                        campaign_dir, _repeat_skeleton(pending))
            except Exception as err:  # this runs FROM an exception handler: if it
                import sys  # raises, it replaces the real stop_reason with its own

                print(f"scqo: could not record the partial repeat "
                      f"({type(err).__name__}: {err})", file=sys.stderr)
        try:
            while True:
                if plan.repeat is not None and repeat_idx >= plan.repeat:
                    stop_reason = "repeat count reached"
                    break
                # The budget gates CONTINUING, never starting: repeat 0 always runs, so
                # a campaign never returns zero measurements for having been slow to set
                # up. Checked BEFORE a repeat, so a repeat is never cut in half either.
                elapsed = time.monotonic() - started_mono
                if (repeat_idx and plan.max_duration_s is not None
                        and elapsed >= plan.max_duration_s):
                    stop_reason = f"max_duration_s ({plan.max_duration_s:g} s) elapsed"
                    status = "stopped"
                    break
                if consecutive_failures >= plan.max_consecutive_failures:
                    stop_reason = (f"{consecutive_failures} consecutive failed repeats "
                                   f"(max_consecutive_failures)")
                    status = "stopped"
                    break
                # Era guard: statistics across cooldowns or setups are not mergeable
                # (the same doctrine as accept()'s). Only reachable on an unbound
                # notebook session, which is exactly where nothing else is guarding.
                if era is not None and self.datastore.run_stamps() != era:
                    stop_reason = "the cooldown/setup changed mid-campaign"
                    status = "stopped"
                    break

                # Cadence: gate the repeat START, so it throttles failed repeats too.
                # A repeat that overran is RECORDED, never padded and never skipped
                # to catch up — the period is a floor, not a schedule to hit.
                overran_by_s = 0.0
                if plan.period_s is not None and repeat_idx:
                    due = started_mono + repeat_idx * plan.period_s
                    wait = due - time.monotonic()
                    if wait > 0:
                        # Announce the gap BEFORE sleeping: an unannounced five-minute
                        # pause is indistinguishable from a hang.
                        on_progress({"kind": "cadence_wait",
                                            "repeat_idx": repeat_idx, "wait_s": wait})
                        time.sleep(wait)
                    else:
                        overran_by_s = -wait

                # repeat_start fires AFTER the sleep, so its timestamp is the truth.
                row = {
                    "repeat_idx": repeat_idx,
                    "started_at": _now(),
                    "elapsed_s": time.monotonic() - started_mono,
                    "overran_by_s": overran_by_s,
                    "steps": [],
                }
                on_progress({"kind": "repeat_start",
                                    "repeat_idx": repeat_idx,
                                    "repeat_planned": plan.repeat,
                                    "n_steps": len(plan.steps),
                                    "started_at": row["started_at"],
                                    "elapsed_s": row["elapsed_s"],
                                    "overran_by_s": overran_by_s,
                                    "durations_s": [r["duration_s"] for r in rows]})
                for step_idx, step in enumerate(plan.steps):
                    step_mono = time.monotonic()
                    try:
                        payload = self.run(
                            step.experiment,
                            plan.step_params(step),
                            update=mode,
                            tags=[*(tags or []), *plan.tags, *step.tags],
                            note=step.note or note or plan.note,
                            skip_artifacts=plan.skip_artifacts,
                            campaign=(campaign_id, repeat_idx, step_idx) if campaign_id else None,
                        )
                    except Exception as err:  # a hard failure is a recorded step error,
                        payload = {"outcomes": {}, "fit": {},  # never a lost campaign
                                   "error": f"{type(err).__name__}: {err}"}
                    outcomes = payload.get("outcomes") or {}
                    step_row = {
                        "step_idx": step_idx,
                        "experiment": step.experiment,
                        "run_id": payload.get("run_id"),
                        "outcomes": outcomes,
                        "fit": payload.get("fit") or {},
                        "error": payload.get("error") or payload.get("datastore_error"),
                        "ok": any(str(o) == "successful" for o in outcomes.values()),
                        "duration_s": time.monotonic() - step_mono,
                    }
                    row["steps"].append(step_row)
                    # Not stoppable HERE: a deliberate stop mid-bundle would leave
                    # quantities that no longer share a drift epoch. An INTERRUPT is
                    # different — the data already exists, so _keep_partial records it.
                    on_progress({"kind": "step_done", "repeat_idx": repeat_idx,
                                        "n_steps": len(plan.steps), **step_row})
                row["ended_at"] = _now()
                row["duration_s"] = time.monotonic() - started_mono - row["elapsed_s"]
                washout = not any(s["ok"] for s in row["steps"])
                row["failed"] = washout
                rows.append(row)
                repeat_idx += 1
                consecutive_failures = consecutive_failures + 1 if washout else 0

                manifest["repeat_done"] = len(rows)
                manifest["repeats_failed"] = sum(1 for r in rows if r["failed"])
                manifest["statistics"] = aggregate(rows)
                if campaign_dir is not None:
                    self.datastore.append_campaign_repeat(campaign_dir, _repeat_skeleton(row))
                    self.datastore.persist_campaign(
                        campaign_id=campaign_id, campaign_dir=campaign_dir, manifest=manifest)

                if washout and plan.on_error == "stop":
                    stop_reason = "a repeat failed and on_error='stop'"
                    status = "stopped"
                    break
                # The ONLY stoppable event: a repeat boundary is the only place where
                # stopping leaves the collected statistics internally consistent.
                if on_progress({"kind": "repeat_done",
                                       "repeat_planned": plan.repeat, **row}) == "stop":
                    stop_reason = "the on_progress callback asked to stop"
                    status = "stopped"
                    break

        except KeyboardInterrupt:
            # Ctrl-C anywhere in the loop, INCLUDING the cadence sleep, which is
            # where a campaign with period_s spends most of its wall clock.
            status, stop_reason = "stopped", "interrupted (Ctrl-C)"
            _keep_partial(row)
        except Exception as err:  # a crash must still leave a readable manifest
            status = "failed"
            stop_reason = f"{type(err).__name__}: {err}"
            _keep_partial(row)
        finally:
            manifest["status"] = status
            manifest["stop_reason"] = stop_reason
            manifest["ended_at"] = _now()
            manifest["repeats"] = [_repeat_skeleton(r) for r in rows]
            # Aggregate writeback: generated on EVERY exit path that measured
            # anything (a stopped overnight campaign still has valid statistics
            # — the statistics.png doctrine), riding the manifest's final
            # write. Own guard: a generation crash must never replace the real
            # stop_reason, exactly like the persist guard below.
            if mode == "none" and manifest["statistics"]:
                try:
                    generated, gen_problems = self._campaign_suggestions(
                        plan, manifest["statistics"])
                    if generated:
                        manifest["suggestions"] = [
                            s.model_dump(mode="json") for s in generated]
                    if gen_problems:
                        manifest["suggestion_problems"] = gen_problems
                except Exception as err:
                    manifest["suggestion_problems"] = [
                        f"generation failed: {type(err).__name__}: {err}"]
            if campaign_dir is not None:
                try:
                    self._finalize_campaign(campaign_id, campaign_dir, manifest)
                except Exception as err:  # a full disk must not replace the real
                    manifest["persist_error"] = (  # stop_reason with its own
                        f"{type(err).__name__}: {err}")
        return manifest

    def _finalize_campaign(self, campaign_id, campaign_dir, manifest) -> None:
        """The last manifest write. ``repeats`` is part of the RETURN value only —
        the per-repeat skeletons already live in ``repeats.jsonl``, and duplicating
        them into the manifest would be a second authority that can disagree."""
        self.datastore.persist_campaign(
            campaign_id=campaign_id, campaign_dir=campaign_dir,
            manifest={k: v for k, v in manifest.items() if k != "repeats"})
        manifest["data_path"] = str(campaign_dir)

    def check_campaign(self, plan: "CampaignPlan | dict") -> list[str]:
        """Pre-flight a plan without running it: problem strings, ``[]`` when clear.

        The same gate :meth:`run_campaign` runs before repeat 0, exposed for
        ``scqo campaign --dry-run``. Touches no instrument and creates nothing."""
        from .campaign import CampaignPlan

        return self._preflight(
            plan if isinstance(plan, CampaignPlan) else CampaignPlan(**dict(plan)))

    def _preflight(self, plan) -> list[str]:
        """Build every step's Parameters and run the roster gate — no hardware.

        Returns problem strings naming the step (plan-level problems first,
        unprefixed), ``[]`` when the plan is clear."""
        from pydantic import ValidationError

        from . import experiments as registry

        problems: list[str] = []
        # A label that shadows a registered experiment name mints campaign ids
        # that read as that experiment's run ids ({stamp}-{device}-{name}-{seq}
        # on both sides) — misleading for accept, provenance and the viewer
        # routes. EXEMPT the degenerate self-named case (a 1-step plan whose
        # only step IS that experiment): `scqo run <name> --repeat N` builds
        # exactly that plan, and there the label truthfully describes the
        # content. Same tolerant lookup as the CLI's _refuse_experiment_name:
        # a driver that fails to import must never break preflight.
        try:
            registry.get(plan.label)
        except Exception:
            pass  # not a registered name (or the registry is broken): fine
        else:
            if not (len(plan.steps) == 1
                    and plan.steps[0].experiment == plan.label):
                problems.append(
                    f"label {plan.label!r} shadows a registered experiment "
                    f"name — pick a different label")
        for step_idx, step in enumerate(plan.steps):
            where = f"step {step_idx} ({step.experiment})"
            try:
                cls = registry.get(step.experiment)
            except KeyError as err:
                problems.append(f"{where}: {err}")
                continue
            defaults = self.parameter_defaults.get(step.experiment, {})
            merged = {**defaults, **plan.step_params(step)}
            try:
                validated = cls.Parameters(**merged)
            except ValidationError as err:
                problems.append(f"{where}: invalid parameters: {err}")
                continue
            exp = cls(self.backend, validated)
            gate_error = self._validate_targets(cls, exp)
            if gate_error is not None:
                problems.append(f"{where}: {gate_error.error}")
        return problems

    def _campaign_suggestions(
        self, plan, statistics: dict,
    ) -> tuple[list[Suggestion], list[str]]:
        """The aggregate-writeback generator: replay each step experiment's
        own ``update()`` on a synthetic Result built from the campaign
        statistics, under a SuggestionCapture — so the fit-key -> field
        routing (including derived knobs, e.g. ``thermalization_time_s``
        from ``t1_s``) keeps its ONE authority, the experiment.

        A plan that runs the same experiment in several steps produced ONE
        merged series (``aggregate()`` slots repeats per experiment), so it
        gets one update() pass, driven by the FIRST such step's parameters.

        Per (experiment, target) the synthetic fit keeps a quantity only when
        its statistic resolved: ``n >= plan.writeback.min_n``, scalar, and
        FINITE — the finiteness gate runs before Suggestion construction
        because ``after`` is non-optional and the datastore's ``_scrub``
        would null a NaN into an unreloadable row. It also accepts the
        JSON-null form the retroactive path reads back. stderr twins pass
        through on purpose: an update() only reads the keys it knows.

        Returns ``(suggestions, problems)``; a failing experiment (e.g. its
        update() KeyErrors on a fit key that did not survive ``min_n``) lands
        in problems and never blocks the others.
        """
        import math

        from . import experiments as registry
        from .result import Outcome
        from .suggestions import SuggestionCapture

        wb = plan.writeback
        suggestions: list[Suggestion] = []
        problems: list[str] = []
        first_step: dict[str, Any] = {}
        for step in plan.steps:
            first_step.setdefault(step.experiment, step)
        for name in dict.fromkeys(plan.experiments):
            fit: dict[str, dict[str, float]] = {}
            for target, quantities in (statistics.get(name) or {}).items():
                kept: dict[str, float] = {}
                for quantity, st in (quantities or {}).items():
                    value = (st or {}).get(wb.stat)
                    if ((st or {}).get("n") or 0) < wb.min_n:
                        continue
                    if (not isinstance(value, (int, float))
                            or isinstance(value, bool)):
                        continue  # nonscalar series / unresolved (JSON null)
                    if not math.isfinite(value):
                        continue  # NaN/Inf must never become a Suggestion
                    kept[quantity] = float(value)
                if kept:
                    fit[target] = kept
            if not fit:
                continue  # nothing resolved for this experiment — not an error
            try:
                cls = registry.get(name)
                merged = {**self.parameter_defaults.get(name, {}),
                          **plan.step_params(first_step[name])}
                exp = cls(self.backend, cls.Parameters(**merged))
                exp.result = cls.Result(
                    outcomes={t: Outcome.SUCCESSFUL for t in fit}, fit=fit)
                capture = SuggestionCapture(self.device, self.physical,
                                            self.roster)
                exp.device = capture
                try:
                    exp.update()
                finally:
                    exp.device = self.device
                for s in capture.suggestions:
                    s.experiment = name
                suggestions.extend(capture.suggestions)
            except Exception as err:
                problems.append(f"{name}: {type(err).__name__}: {err}")
        return suggestions, problems

    def find_campaigns(self, **filters: Any) -> list[dict]:
        """Query saved campaigns (newest first); ``[]`` without a data_root."""
        if self.datastore is None:
            return []
        return self.datastore.find_campaigns(**filters)

    def load_campaign(self, campaign_id: str) -> dict:
        """One campaign's manifest + repeat skeleton (see DataStore.load_campaign)."""
        return self._require_datastore().load_campaign(campaign_id)

    def campaign_runs(self, campaign_id: str) -> list[dict]:
        """A campaign's children in execution order (repeat_idx, step_idx)."""
        return self._require_datastore().campaign_runs(campaign_id)

    def suggest_campaign(self, campaign_id: str, *, force: bool = False) -> dict:
        """(Re)generate a FINISHED campaign's aggregate-writeback suggestions
        from its stored statistics — for campaigns that predate the writeback
        feature, or after rejecting a batch. The plan (including its
        ``[writeback]`` policy) is rebuilt from the manifest, so the result is
        exactly what finalize would have produced.

        Refuses a still-running campaign: ``persist_campaign`` is a lockless
        whole-file write from the running process, so anything written here
        mid-run would be silently erased on its next repeat. Refuses to
        overwrite an existing suggestion set — decisions are part of it —
        unless ``force``."""
        from .campaign import CampaignPlan

        store = self._require_datastore()
        manifest = store.load_campaign(campaign_id)["manifest"]
        device = manifest.get("device", "")
        if device and device != store.device_name:
            raise RuntimeError(
                f"campaign {campaign_id} belongs to device {device!r} but "
                f"this session is bound to {store.device_name!r}")
        if manifest.get("status") == "running" or not manifest.get("ended_at"):
            raise RuntimeError(
                f"campaign {campaign_id} is still running — its process "
                f"rewrites campaign.json after every repeat and would erase "
                f"anything written now; suggestions are generated when it "
                f"finishes")
        if manifest.get("suggestions") and not force:
            raise RuntimeError(
                f"campaign {campaign_id} already has "
                f"{len(manifest['suggestions'])} suggestions (decisions "
                f"included) — pass force=True (--force) to regenerate")
        plan = CampaignPlan(**manifest["plan"])
        generated, problems = self._campaign_suggestions(
            plan, manifest.get("statistics") or {})
        new_dicts = [s.model_dump(mode="json") for s in generated]
        stored = store.edit_campaign_suggestions(
            campaign_id, lambda _rows: new_dicts, problems=problems)
        return {
            "campaign_id": campaign_id,
            "suggestions": len(new_dicts),
            "problems": problems,
            "pending_total": pending_count(stored.get("suggestions", [])),
        }

    def _validate_targets(self, cls, exp):
        """Roster gate before any hardware: targets exist, their KIND is in
        the experiment's ``target_kinds``, and ``required_operations`` are
        carried (derived from wiring for modes, declared for composites).
        A ``flux_component`` Parameter must name an entity with a default
        flux channel — kind-agnostic, so qubit z-lines and coupler z-lines
        gate identically."""
        from .result import Outcome

        targets = list(getattr(exp.params, "targets", []))
        problems: list[str] = []
        want_kinds = set(cls.target_kinds)
        want_ops = set(cls.required_operations)
        flux_component = getattr(exp.params, "flux_component", None)
        if flux_component is not None:
            want_ops -= {"flux_bias"}  # the assigned source actuates instead
            if flux_component not in self.roster:
                problems.append(f"flux_component {flux_component!r}: not in "
                                f"this device's roster")
            elif (flux_component, "flux") not in self.roster.defaults:
                problems.append(
                    f"flux_component {flux_component!r}: has no flux "
                    f"channel (nothing to sweep)")
        for t in targets:
            if t not in self.roster:
                problems.append(
                    f"{t}: not in this device's roster "
                    f"({', '.join(sorted(self.roster.entities))})")
                continue
            e = self.roster.entities[t]
            if e.kind not in want_kinds:
                problems.append(f"{t}: is kind {e.kind!r}, {cls.name} "
                                f"targets {sorted(want_kinds)}")
                continue
            try:
                have_ops = set(self.roster.operations(t))
            except Exception:
                have_ops = set()
            missing = want_ops - have_ops
            if missing:
                problems.append(
                    f"{t}: lacks operation(s) {sorted(missing)} "
                    f"(carried: {sorted(have_ops) or 'none'})")
        problems += cls.validate_targets(self.roster, targets)
        if not problems:
            return None
        return cls.Result(
            outcomes={t: Outcome.FAILED for t in targets},
            error="target validation refused the run before any hardware: "
                  + "; ".join(problems))

    @staticmethod
    def _failure(cls, exp, err):
        from .contract import ContractError
        from .result import Outcome

        outcome = (Outcome.NO_DATA if isinstance(err, ContractError)
                   else Outcome.FAILED)
        targets = getattr(exp.params, "targets", [])
        return cls.Result(outcomes={t: outcome for t in targets},
                          error=_describe_error(err))

    def _invalid_params(self, cls, merged, defaults, caller, err):
        from .result import Outcome

        hints = []
        for detail in err.errors():
            key = str(detail["loc"][0]) if detail.get("loc") else ""
            if key and key in defaults and key not in caller:
                source = (self.parameter_defaults_source
                          or "the session's parameter_defaults")
                hints.append(f"'{key}' came from {source} [{cls.name}] — "
                             f"fix it there")
        targets = merged.get("targets")
        targets = ([t for t in targets if isinstance(t, str)]
                   if isinstance(targets, list) else [])
        message = f"invalid parameters for {cls.name!r}: {err}"
        if hints:
            message += "\n" + "\n".join(hints)
        return cls.Result(outcomes={t: Outcome.FAILED for t in targets},
                          error=message)

    # ------------------------------------------------------------- writing

    def _write_state(self, entity: str, field: str, value) -> None:
        """One knob/monitor write through the recording device (push-first
        for knobs; record-only for monitors)."""
        view = self.device.component(entity)
        if isinstance(view, CompositeView):
            view.write_knob(field, value)
        else:
            setattr(view, field, value)

    def _apply(
        self,
        suggestions: list[Suggestion],
        *,
        experiment: str | None,
        run_id: str | None,
        campaign_id: str | None = None,
        comment: str = "",
        reapply: bool = False,
    ) -> tuple[list[Suggestion], list[str]]:
        """Apply PENDING suggestions through the real stores; mutates their
        statuses. Facts go to the physical store, knobs/monitors through the
        recording device (vendor-push-FIRST). Per-entity atomicity: one
        failed item skips that entity's REMAINING items; other entities
        proceed. Does NOT save; the caller persists."""
        applied: list[Suggestion] = []
        errors: list[str] = []
        eligible = [s for s in suggestions
                    if s.status == "pending" or reapply]
        # Group per entity, ordered by catalog field position within the
        # group: a stored [waveform, dt] pair applies dt-first (the same
        # ordering doctrine as the push order), and the group's relation
        # consistency is pre-checked so a doomed group fails WHOLE, with the
        # cause on every item, before any of it touches a store.
        groups: dict[str, list[Suggestion]] = {}
        for s in eligible:
            groups.setdefault(s.entity, []).append(s)
        self.device.set_context(experiment, run_id, campaign_id=campaign_id)
        try:
            for entity, group in groups.items():
                order = {f: i for i, f
                         in enumerate(self.roster.fields_of(entity))}
                group.sort(key=lambda s: order.get(s.field, len(order)))
                try:
                    self._validate_batch(
                        [(s.entity, s.field,
                          self.roster.fields_of(s.entity)[s.field], s.after)
                         for s in group])
                except ValueError as err:
                    for s in group:
                        s.comment = f"apply failed: {err}"
                    errors.append(f"{entity}: {err}")
                    continue
                for s in group:
                    try:
                        if s.role == "fact":
                            self.physical.record(s.entity, s.field, s.after,
                                                 experiment=experiment,
                                                 run_id=run_id,
                                                 campaign_id=campaign_id)
                        else:
                            self._write_state(s.entity, s.field, s.after)
                    except Exception as err:
                        s.comment = (f"apply failed: "
                                     f"{type(err).__name__}: {err}")
                        errors.append(f"{s.entity}.{s.field}: "
                                      f"{type(err).__name__}: {err}")
                        break  # no half-applied entity
                    s.status = "accepted"
                    s.decided_at = _now()
                    s.decided_by = _current_operator() or None
                    if comment:
                        s.comment = comment
                    applied.append(s)
        finally:
            self.device.set_context(None, None)
        return applied, errors

    # -------------------------------------------------------- suggest/accept

    def accept(
        self,
        run_id: str,
        *,
        entities: list[str] | None = None,
        fields: list[str] | None = None,
        indices: list[int] | None = None,
        comment: str = "",
        force: bool = False,
        reapply: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Apply a saved run's pending suggested updates — possibly days
        later. Era guard (the run's cooldown/setup must match the current
        one) and staleness guard (each item's ``before`` must equal the
        store's CURRENT value) protect a deferred apply unless ``force``;
        ``reapply`` re-decides already-decided items with staleness OFF (a
        rollback deliberately overwrites). ``dry_run`` evaluates and reports
        without mutating anything."""
        store, record = self._load_run_record(run_id)
        suggestions = load_suggestions(record.get("suggestions", []))
        selected = select_suggestions(
            suggestions, entities=entities, fields=fields, indices=indices,
            include_decided=reapply or dry_run)

        run_era = (record.get("cooldown", ""), record.get("setup", ""))
        if dry_run or not force:
            current_era = store.run_stamps()
            era = {"run": list(run_era), "current": list(current_era),
                   "match": run_era == current_era}
        else:
            era = {"run": list(run_era), "current": None, "match": True}

        device_snapshot = self.device.snapshot()
        items: list[dict[str, Any]] = []
        for i in selected:
            s = suggestions[i]
            current = (self.physical.get(s.entity, s.field)
                       if s.role == "fact"
                       else device_snapshot.get(s.entity, {}).get(s.field))
            items.append({
                "index": i, "entity": s.entity, "field": s.field,
                "role": s.role, "status": s.status, "before": s.before,
                "current": current, "after": s.after,
                "stale": current != s.before, "decided_at": s.decided_at,
                "decided_by": s.decided_by, "comment": s.comment,
            })
        if dry_run:
            return {"run_id": run_id, "era": era, "items": items}

        summary: dict[str, Any] = {"run_id": run_id, "applied": [],
                                   "stale": [], "errors": []}
        if not selected:
            summary["pending_left"] = pending_count(suggestions)
            return summary

        if not force and not era["match"]:
            raise RuntimeError(
                f"run {run_id} was measured under cooldown/setup {run_era} "
                f"but the device is now on {tuple(era['current'])} — its "
                f"values may not transfer; use force=True (--force) to "
                f"apply anyway")

        to_apply: list[Suggestion] = []
        current_of: dict[int, Any] = {}
        for item in items:
            s = suggestions[item["index"]]
            current_of[id(s)] = item["current"]
            # A reapply deliberately overwrites a newer value, so staleness
            # cannot be an error there.
            if not force and not reapply and item["stale"]:
                summary["stale"].append(
                    {"entity": s.entity, "field": s.field,
                     "before": s.before, "current": item["current"],
                     "after": s.after})
                continue
            to_apply.append(s)

        applied, errors = self._apply(
            to_apply, experiment=record.get("experiment"), run_id=run_id,
            comment=comment, reapply=reapply)
        summary["errors"] = errors
        summary["applied"] = [
            {"entity": s.entity, "field": s.field, "role": s.role,
             "before": s.before, "current": current_of.get(id(s)),
             "after": s.after}
            for s in applied]
        # From here on nothing may raise: the vendor already carries the
        # applied values, so the decision MUST reach record.json.
        if applied:
            try:
                if self._persist:
                    self.device.save()
                self.physical.save()
            except Exception as err:
                summary["errors"].append(
                    f"values were applied to the device but saving state "
                    f"failed (state files may lag the instrument): "
                    f"{type(err).__name__}: {err}")
        try:
            stored = store.edit_suggestions(
                run_id,
                decision_editor({i: suggestions[i].model_dump(mode="json")
                                 for i in selected}),
                updated_device=True if applied else None)
            summary["pending_left"] = pending_count(stored["suggestions"])
        except Exception as err:
            summary["errors"].append(
                f"values were applied to the device but persisting the "
                f"decision failed — record.json still lists them as pending "
                f"(a blind retry could double-apply): "
                f"{type(err).__name__}: {err}")
            summary["pending_left"] = pending_count(suggestions)
        return summary

    def reject(self, run_id: str, *, entities: list[str] | None = None,
               fields: list[str] | None = None,
               indices: list[int] | None = None, comment: str = "") -> dict:
        """Decline pending suggestions (metadata only)."""
        return reject_suggestions(
            self._require_datastore(), run_id, entities=entities,
            fields=fields, indices=indices, comment=comment)

    def accept_campaign(
        self,
        campaign_id: str,
        *,
        entities: list[str] | None = None,
        fields: list[str] | None = None,
        indices: list[int] | None = None,
        comment: str = "",
        force: bool = False,
        reapply: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Apply a campaign's pending aggregate suggestions — the campaign
        twin of :meth:`accept`. Era guard (the campaign's cooldown/setup must
        match the current one) and staleness guard (each item's ``before``
        was captured at finalize and must equal the store's CURRENT value)
        protect the deferred apply unless ``force``; ``reapply`` re-decides
        already-decided items with staleness OFF. Values apply GROUPED by
        proposing experiment — one :meth:`_apply` pass per group, stamped
        ``(experiment, campaign_id)`` with NO run_id, so provenance reads
        status "campaign" — and the state saves ONCE after all groups."""
        store = self._require_datastore()
        manifest = store.load_campaign(campaign_id)["manifest"]
        device = manifest.get("device", "")
        if device and device != store.device_name:
            raise RuntimeError(
                f"campaign {campaign_id} belongs to device {device!r} but "
                f"this session is bound to {store.device_name!r}")
        suggestions = load_suggestions(manifest.get("suggestions", []))
        selected = select_suggestions(
            suggestions, entities=entities, fields=fields, indices=indices,
            include_decided=reapply or dry_run)

        campaign_era = (manifest.get("cooldown") or "",
                        manifest.get("setup") or "")
        if dry_run or not force:
            current_era = store.run_stamps()
            era = {"campaign": list(campaign_era),
                   "current": list(current_era),
                   "match": campaign_era == current_era}
        else:
            era = {"campaign": list(campaign_era), "current": None,
                   "match": True}

        device_snapshot = self.device.snapshot()
        items: list[dict[str, Any]] = []
        for i in selected:
            s = suggestions[i]
            current = (self.physical.get(s.entity, s.field)
                       if s.role == "fact"
                       else device_snapshot.get(s.entity, {}).get(s.field))
            items.append({
                "index": i, "entity": s.entity, "field": s.field,
                "role": s.role, "experiment": s.experiment,
                "status": s.status, "before": s.before,
                "current": current, "after": s.after,
                "stale": current != s.before, "decided_at": s.decided_at,
                "decided_by": s.decided_by, "comment": s.comment,
            })
        if dry_run:
            return {"campaign_id": campaign_id, "era": era, "items": items}

        summary: dict[str, Any] = {"campaign_id": campaign_id, "applied": [],
                                   "stale": [], "errors": []}
        if not selected:
            summary["pending_left"] = pending_count(suggestions)
            return summary

        if not force and not era["match"]:
            raise RuntimeError(
                f"campaign {campaign_id} was measured under cooldown/setup "
                f"{campaign_era} but the device is now on "
                f"{tuple(era['current'])} — its values may not transfer; use "
                f"force=True (--force) to apply anyway")

        to_apply: list[Suggestion] = []
        current_of: dict[int, Any] = {}
        for item in items:
            s = suggestions[item["index"]]
            current_of[id(s)] = item["current"]
            # A reapply deliberately overwrites a newer value, so staleness
            # cannot be an error there.
            if not force and not reapply and item["stale"]:
                summary["stale"].append(
                    {"entity": s.entity, "field": s.field,
                     "before": s.before, "current": item["current"],
                     "after": s.after})
                continue
            to_apply.append(s)

        # One _apply pass per proposing experiment (first-seen order): the
        # ChangeRecord.experiment stamp stays truthful per row, and per-entity
        # atomicity holds within each group.
        by_experiment: dict[str | None, list[Suggestion]] = {}
        for s in to_apply:
            by_experiment.setdefault(s.experiment, []).append(s)
        applied: list[Suggestion] = []
        for exp_name, group in by_experiment.items():
            done, errors = self._apply(
                group, experiment=exp_name, run_id=None,
                campaign_id=campaign_id, comment=comment, reapply=reapply)
            applied.extend(done)
            summary["errors"].extend(errors)
        summary["applied"] = [
            {"entity": s.entity, "field": s.field, "role": s.role,
             "experiment": s.experiment, "before": s.before,
             "current": current_of.get(id(s)), "after": s.after}
            for s in applied]
        # From here on nothing may raise: the vendor already carries the
        # applied values, so the decision MUST reach campaign.json.
        if applied:
            try:
                if self._persist:
                    self.device.save()
                self.physical.save()
            except Exception as err:
                summary["errors"].append(
                    f"values were applied to the device but saving state "
                    f"failed (state files may lag the instrument): "
                    f"{type(err).__name__}: {err}")
        try:
            stored = store.edit_campaign_suggestions(
                campaign_id,
                decision_editor({i: suggestions[i].model_dump(mode="json")
                                 for i in selected}))
            summary["pending_left"] = pending_count(
                stored.get("suggestions", []))
        except Exception as err:
            summary["errors"].append(
                f"values were applied to the device but persisting the "
                f"decision failed — campaign.json still lists them as "
                f"pending (a blind retry could double-apply): "
                f"{type(err).__name__}: {err}")
            summary["pending_left"] = pending_count(suggestions)
        return summary

    def reject_campaign(self, campaign_id: str, *,
                        entities: list[str] | None = None,
                        fields: list[str] | None = None,
                        indices: list[int] | None = None,
                        comment: str = "") -> dict:
        """Decline a campaign's pending suggestions (metadata only)."""
        return reject_campaign_suggestions(
            self._require_datastore(), campaign_id, entities=entities,
            fields=fields, indices=indices, comment=comment)

    def suggest(self, run_id: str, assignments: dict[str, Any],
                comment: str = "") -> dict:
        """Attach YOUR manually-read values to a saved run as pending
        suggestions (origin="operator"); decided exactly like estimator
        suggestions. ``before`` is captured NOW — what the staleness guard
        compares at accept time."""
        if not assignments:
            raise ValueError(
                "no assignments given — expected {'entity.field': value}")
        store, record = self._load_run_record(run_id)
        if any("entity" not in r for r in record.get("suggestions", [])):
            raise ValueError(
                f"run {run_id}'s stored suggestions are pre-greenfield rows "
                f"(display-only) — appending would make yours undecidable "
                f"too; propose against a new run")
        proposed_at = _now()
        new: list[Suggestion] = []
        for entity, field, spec, value in self._parse_assignments(
                assignments, relations=False):
            new.append(Suggestion(
                entity=entity, field=field, role=spec.role,
                kind=self.roster.entities[entity].kind, unit=spec.unit,
                before=self._current_value(entity, field, spec.role),
                after=value, comment=comment, origin="operator",
                proposed_by=_current_operator() or None,
                proposed_at=proposed_at))
        new_dicts = [s.model_dump(mode="json") for s in new]
        stored = store.edit_suggestions(run_id, lambda fresh: fresh + new_dicts)
        return {
            "run_id": run_id,
            "added": [{"entity": s.entity, "field": s.field, "role": s.role,
                       "before": s.before, "after": s.after} for s in new],
            "pending_total": pending_count(stored["suggestions"]),
        }

    def set_values(self, assignments: dict[str, Any], *,
                   dry_run: bool = False) -> dict:
        """Write operator-known values directly — the RUNLESS counterpart of
        suggest. ALL assignments validate before ANYTHING is written; writes
        go through the normal stores immediately (knobs vendor-push-FIRST,
        ChangeRecord with experiment=None — the ``(manual)`` provenance).
        Per-entity atomicity as in accept."""
        if not assignments:
            raise ValueError(
                "no assignments given — expected {'entity.field': value}")
        validated = self._parse_assignments(assignments)

        if dry_run:
            return {"items": [
                {"entity": n, "field": f, "role": spec.role,
                 "unit": spec.unit,
                 "current": self._current_value(n, f, spec.role), "after": v}
                for n, f, spec, v in validated]}

        applied: list[dict[str, Any]] = []
        errors: list[str] = []
        failed_entities: set[str] = set()
        for entity, field, spec, value in validated:
            if entity in failed_entities:
                continue
            # `before` is read live right before the write: an earlier item
            # of this call — or its coupled echo — may have moved it.
            before = self._current_value(entity, field, spec.role)
            try:
                if spec.role == "fact":
                    self.physical.record(entity, field, value)
                else:
                    self._write_state(entity, field, value)
            except Exception as err:
                failed_entities.add(entity)  # no half-applied entity
                errors.append(
                    f"{entity}.{field}: {type(err).__name__}: {err}")
                continue
            applied.append({"entity": entity, "field": field,
                            "role": spec.role, "before": before,
                            "after": value})
        summary: dict[str, Any] = {"applied": applied, "errors": errors}
        if applied:
            try:
                if self._persist:
                    self.device.save()
                self.physical.save()
            except Exception as err:
                summary["errors"].append(
                    f"values were applied to the device but saving state "
                    f"failed (state files may lag the instrument): "
                    f"{type(err).__name__}: {err}")
        return summary

    # ------------------------------------------------------------ plumbing

    def _parse_assignments(self, assignments: dict[str, Any], *,
                           relations: bool = True):
        """Shared suggest/set_values validation: every key is
        ``entity.field`` resolved through the qubit-closure sugar; every
        value passes the owning store's shape/finiteness validation. ALL
        assignments validate before anything is written; two keys that
        alias one resolved (entity, field) are refused.

        ``relations=True`` (set_values — a direct write must be immediately
        consistent) additionally validates the batch AS A SEQUENCE: the
        waveform-dt prerequisite per step (satisfiable by ordering the dict
        dt-first), and paired-array lengths at batch END — so a redone fit
        may change both partners' length in ONE call. ``relations=False``
        (suggest — a proposal's partner may itself only be proposed) skips
        that; the accept re-checks when it records."""
        out = []
        resolved: dict[tuple[str, str], str] = {}
        for key, value in assignments.items():
            name, _, field = key.partition(".")
            if not name or not field:
                raise ValueError(
                    f"assignment key {key!r} must be 'entity.field' "
                    f"(e.g. q1.pi_amp, q1_res.f_r_hz)")
            try:
                entity, spec = self.roster.resolve_field(name, field)
            except Exception as err:
                raise ValueError(str(err)) from None
            prior = resolved.get((entity, field))
            if prior is not None:
                raise ValueError(
                    f"{key!r} and {prior!r} both resolve to "
                    f"{entity}.{field} — one assignment per field")
            resolved[(entity, field)] = key
            store = self.physical if spec.role == "fact" else self.state
            try:
                value = store.check(entity, field, value, relations=False)
            except Exception as err:
                raise ValueError(str(err)) from None
            out.append((entity, field, spec, value))
        if relations:
            self._validate_batch(out)
        return out

    def _batch_reader(self, overlay: dict):
        def current(entity: str, field: str):
            if field in overlay.get(entity, {}):
                return overlay[entity][field]
            spec = self.roster.fields_of(entity)[field]
            store = self.physical if spec.role == "fact" else self.state
            return store.get(entity, field)
        return current

    def _validate_batch(self, items) -> None:
        """Relation consistency of the WHOLE batch before anything is
        written: waveform-dt per step against the batch overlay, paired
        lengths at batch END per touched entity."""
        overlay: dict[str, dict[str, Any]] = {}
        current = self._batch_reader(overlay)
        for entity, field, spec, value in items:
            if (spec.shape == "float[]" and field.endswith("_waveform")
                    and current(entity, f"{field}_dt_s") is None):
                raise ValueError(
                    f"{entity}.{field}: set {field}_dt_s first (in the same "
                    f"call is fine — order the assignments dt before "
                    f"waveform)")
            overlay.setdefault(entity, {})[field] = value
        for entity in overlay:
            for field, spec in self.roster.fields_of(entity).items():
                if not spec.paired_with:
                    continue
                a = current(entity, field)
                b = current(entity, spec.paired_with)
                if a is not None and b is not None and len(a) != len(b):
                    raise ValueError(
                        f"{entity}.{field}/{spec.paired_with}: the batch "
                        f"would leave unequal paired lengths ({len(a)} != "
                        f"{len(b)}) — change both sides in one call")

    def _current_value(self, entity: str, field: str, role: str):
        if role == "fact":
            return self.physical.get(entity, field)
        return self.device.snapshot().get(entity, {}).get(field)

    def _load_run_record(self, run_id: str) -> tuple[DataStore, dict]:
        store = self._require_datastore()
        record = store.load_run(run_id)["record"]
        if record.get("device") != store.device_name:
            raise RuntimeError(
                f"run {run_id} belongs to device {record.get('device')!r} "
                f"but this session is bound to {store.device_name!r}")
        return store, record

    def _require_datastore(self) -> DataStore:
        if self.datastore is None:
            raise RuntimeError(
                "this Session has no data_root configured (no datastore)")
        return self.datastore

    # ----------------------------------------------------------- datastore

    def find_runs(self, **filters: Any) -> list[dict]:
        """Query saved runs (newest first); [] without a data_root."""
        if self.datastore is None:
            return []
        return self.datastore.find_runs(**filters)

    def load_run(self, run_id: str) -> dict:
        return self._require_datastore().load_run(run_id)

    def tag_run(self, run_id: str, *, add: list[str] | None = None,
                remove: list[str] | None = None,
                note: str | None = None) -> dict:
        return self._require_datastore().tag_run(run_id, add=add,
                                                 remove=remove, note=note)

    # --------------------------------------------------------------- state

    def device_state(self) -> dict:
        """The operating state per entity (knobs + monitors)."""
        return self.device.snapshot()

    def physical_state(self) -> dict:
        """The sample's measured physics for THIS context."""
        return self.physical.values()

    def qubit_state(self, name: str) -> dict:
        """The per-qubit ASSEMBLED view: the mode's facts plus every closure
        member (default channels, attached resonator) — grouping is derived
        at read time from refs, never declared."""
        e = self.roster.entities.get(name)
        if e is None:
            raise KeyError(f"unknown entity {name!r}")
        from .entities import Mode
        if not isinstance(e, Mode):
            hint = (f" — did you mean {e.target[0]!r}?"
                    if getattr(e, "target", None) and len(e.target) == 1
                    else "")
            raise KeyError(
                f"qubit_state takes a mode; {name!r} is a "
                f"{type(e).__name__.lower()}{hint}")
        out: dict[str, dict] = {}
        physical = self.physical.values()
        state = self.device.snapshot()
        members = [name]
        members += [c.name for c in self.roster.channels_of(name)
                    if self.roster.defaults.get((name, c.kind)) == c.name]
        members += [m.name for m in self.roster.modes().values()
                    if m.refs.get("qubit") == name]
        for member in members:
            merged = {**physical.get(member, {}), **state.get(member, {})}
            if merged:
                out[member] = merged
        return out

    def history(self, store: str = "state") -> list[dict]:
        """The recorded change history (the loop's memory): ``"state"``
        (knobs + monitors) or ``"physical"`` (measured facts)."""
        if store == "physical":
            return [r.as_dict() for r in self.physical.history()]
        if store != "state":
            raise ValueError(
                f"store must be 'state' or 'physical', got {store!r}")
        return [r.as_dict() for r in self.device.history()]
