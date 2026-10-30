"""Exact native resume binding for the managed TUI.

A native ``SessionStart(source=resume)`` names one exact SID, JSONL path and
working directory.  This module is the single place that proves and adopts that
identity: workspace provenance comes from the resumed history itself or from an
exact verified catalogue registration; authority comes from the source's own
genuine user records, or, for host-only windows, from a strictly verified
inherited lineage.  A validated event that cannot be proved yet is recorded for
exact reobservation; it is never replaced by a later hook identity, replayed, or
force-bound.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from . import core
from .context_runtime import ContextRuntime, ContextRuntimeError, _source, _uuid
from .history import HistoryError, redact


UNBOUND_RESUME_WAIT = "resume_event_unbound_pending"
_RESUME_IDENTITY = ("session_id", "transcript_path", "cwd")
_MAIN_HOOKS = frozenset({"UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
                         "PostToolBatch", "Stop", "StopFailure"})


def _owner_workspace_matches(state, cwd):
    return str(cwd) in {state.get("cwd"), state.get("launch_cwd", state.get("cwd"))}


def _history_stamp(path):
    """Cheap change marker for one exact transcript; never a proof by itself."""
    try:
        stat = Path(path).stat()
    except (OSError, TypeError, ValueError):
        return None
    return [stat.st_ino, stat.st_size, stat.st_mtime_ns]


class ResumeBindingMixin:
    """Resume, source-catalogue and lineage binding for ``TuiRuntime``.

    Host requirements: the ``ContextRuntime`` state, wait, catalogue, usage and
    authorization primitives, plus ``_superseded_lifecycle``, ``_window`` and
    ``_session_start_transition`` from the TUI controller.
    """

    @staticmethod
    def _same_resume_binding(state, sid, source_path, cwd):
        return (isinstance(state, dict) and state.get("session_id") == sid
                and state.get("source_path") == source_path and state.get("cwd") == cwd)

    @staticmethod
    def _unbound_resume(state):
        """Return a validated resume event that this manager has not adopted yet."""
        pending = state.get("pending_session_start")
        return pending if isinstance(pending, dict) and pending.get("bound") is False else None

    @staticmethod
    def _catalogue_floor(state, entries):
        rotation = state.get("rotation")
        if not isinstance(rotation, dict):
            raise ContextRuntimeError("恢复窗口 rotation 无效")
        values = [entry["generation"] for entry in entries]
        for value in (state.get("window_generation"), rotation.get("generation")):
            if type(value) is not int or value < 0:
                raise ContextRuntimeError("恢复窗口 generation 无效")
            values.append(value)
        return max(values, default=0)

    def _catalogue_entries(self, runtime):
        """Read only canonical reference entries and validate each through _catalogue_source."""
        history = core.safe_path(runtime.directory, "history", exists=False)
        if not history.exists():
            return []
        if not history.is_dir() or history.is_symlink():
            raise ContextRuntimeError("native history catalogue is unavailable")
        entries = []
        for path in sorted(history.glob("*.json"), key=lambda item: item.name):
            name = path.name
            if not (name.endswith(".json") and len(name) > 14 and name[:8].isdigit() and name[8] == "-"):
                continue
            try:
                expected_generation = int(name[:8])
                expected_sid = _uuid(name[9:-5], "catalogue filename session_id")
            except ContextRuntimeError:
                continue
            try:
                raw = core.read_json(core.safe_path(runtime.directory, path))
                if not isinstance(raw, dict):
                    raise ContextRuntimeError("native history catalogue entry is invalid")
                probe = {"session_id": raw.get("session_id"), "source_path": raw.get("source_path"),
                         "window_generation": raw.get("generation")}
                binding = runtime._catalogue_source(probe, require_existing=True)
            except (OSError, TypeError, ValueError, KeyError) as exc:
                raise ContextRuntimeError("native history catalogue entry is invalid") from exc
            expected_path = history / f"{expected_generation:08d}-{expected_sid}.json"
            if raw != binding or path != expected_path:
                raise ContextRuntimeError("native history catalogue entry is invalid")
            entries.append(binding)
        return sorted(entries, key=lambda entry: (entry["generation"], entry["session_id"], entry["source_path"]))

    def _resume_candidate(self, state, incoming_state, sid, source_path):
        """Adopt a validated native epoch before its JSONL has finished appearing."""
        entries = self._catalogue_entries(self)
        floor = self._catalogue_floor(state, entries)
        known = [entry["generation"] for entry in entries
                 if (entry["session_id"], entry["source_path"]) == (sid, source_path)]
        incoming_generation = incoming_state.get("window_generation") if isinstance(incoming_state, dict) else None
        if (self._same_resume_binding(incoming_state, sid, source_path, state["cwd"])
                and type(incoming_generation) is int and incoming_generation in known
                and incoming_generation >= floor):
            generation = incoming_generation
        else:
            generation = floor + 1
        candidate = deepcopy(state)
        candidate["superseded_lifecycle"] = self._superseded_lifecycle(state)
        candidate["phase"] = "running"
        candidate["rotation"] = {"generation": max(floor, generation), "request": None, "clear": None}
        candidate["window_generation"] = generation
        candidate["session_id"], candidate["source_path"], candidate["authorization"] = sid, source_path, None
        candidate["native_context_window"] = None
        candidate["usage"], candidate["budget_stream"], candidate["budget_handoff_signal"] = None, None, None
        candidate["budget"] = {}
        # Receipts reference persisted submitted inputs; a resume is not proof
        # that those inputs were consumed.  Preserve them without replaying text.
        candidate["deferred_inputs"] = deepcopy(state.get("deferred_inputs", []))
        candidate["pending_tool_ids"], candidate["active_child_handles"], candidate["background_tool_ids"] = [], [], []
        candidate["pending_tool_sources"], candidate["child_handle_sources"] = {}, {}
        candidate["at_turn_boundary"] = False
        for key in ("tool_batch_boundary", "rotation_model", "rotation_permission",
                    "rotation_deadline", "continuation_hash", "continuation_observed", "continuation_input_receipts",
                    "continuation_stop_serial", "diagnostic",
                    "wait_notice_key",
                    "stop_observed_at", "stop_text_hash", "stop_serial", "stop_turn_generation", "stop_snapshot",
                    "failure_boundary", "context_failure", "native_prompt_id", "input_observed_after_byte",
                    "settlement_stop_serial", "resume_safe_boundary", "resume_snapshot",
                    "resume_historical_pending_tool_ids", "reconciliation"):
            candidate.pop(key, None)
        return candidate

    def _lineage_catalogue_entries(self, owner, owner_state, authorization):
        """Return every validated reference from the root window through the owner's exact current source."""
        entries = self._catalogue_entries(owner)
        current = (owner_state["session_id"], owner_state["source_path"])
        current_generation = owner_state["window_generation"]
        if not any((entry["session_id"], entry["source_path"], entry["generation"])
                   == (*current, current_generation) for entry in entries):
            raise ContextRuntimeError("恢复来源未登记为 owner 的当前原生历史")
        root = authorization["root_instruction_locator"]
        latest = authorization["latest_instruction_locator"]
        root_pair = (root["session_id"], root["source_path"])
        latest_pair = (latest["session_id"], latest["source_path"])
        root_generations = [entry["generation"] for entry in entries
                            if (entry["session_id"], entry["source_path"]) == root_pair
                            and entry["generation"] <= current_generation]
        latest_generations = [entry["generation"] for entry in entries
                              if (entry["session_id"], entry["source_path"]) == latest_pair
                              and entry["generation"] <= current_generation]
        if not root_generations or not latest_generations:
            raise ContextRuntimeError("授权 lineage 缺少已登记的原生历史")
        first_generation = min(root_generations)
        if first_generation > current_generation:
            raise ContextRuntimeError("授权 lineage generation 不连续")
        references = [entry for entry in entries
                      if first_generation <= entry["generation"] <= current_generation]
        present = {(entry["session_id"], entry["source_path"]) for entry in references}
        if root_pair not in present or latest_pair not in present or current not in present:
            raise ContextRuntimeError("授权 lineage 历史不完整")
        return references

    def _owner_states(self):
        """Yield readable sibling context states; unreadable ones are neither proof nor conflict."""
        root = self.directory.parent
        try:
            directories = sorted(root.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise ContextRuntimeError("无法枚举既有私有 context 状态") from exc
        for directory in directories:
            if directory == self.directory or not directory.is_dir() or directory.is_symlink():
                continue
            try:
                owner = ContextRuntime(_uuid(directory.name, "context directory"),
                                       configuration_reader=self.configuration_reader)
                if owner.directory != directory:
                    continue
                owner_state = owner._state()
            except (OSError, TypeError, ValueError, KeyError):
                continue
            yield owner, owner_state

    def _resolve_resume_authorization(self, incoming_state, source, sid, cwd):
        """Resolve only an exact, settled owner lineage for a host-only native source."""
        source_path = str(source.path)
        owners = []

        def consider(owner, owner_state):
            if not isinstance(owner_state, dict):
                return
            if owner_state.get("session_id") != sid or owner_state.get("source_path") != source_path:
                return
            if owner.conversation_id != self.conversation_id and not _owner_workspace_matches(owner_state, cwd):
                raise ContextRuntimeError("恢复来源与已登记 owner 的工作目录不符")
            rotation = owner_state.get("rotation")
            if not isinstance(rotation, dict) or type(owner_state.get("window_generation")) is not int:
                raise ContextRuntimeError("恢复 owner 状态无效")
            # Authority lineage is a verified historical fact.  A stale clear,
            # direct tool receipt, or continuation marker decides only whether an
            # old command may run; it must not prevent binding the real resumed
            # native epoch.
            authorization = owner_state.get("authorization")
            if authorization is None:
                return
            owner._catalogue_source(owner_state, require_existing=True)
            owner._verify_authorization(owner_state)
            owners.append((owner.conversation_id, {
                "root_instruction_locator": deepcopy(authorization["root_instruction_locator"]),
                "latest_instruction_locator": deepcopy(authorization["latest_instruction_locator"]),
            }, self._lineage_catalogue_entries(owner, owner_state, authorization)))

        consider(self, incoming_state)
        # Once this manager has adopted the new epoch, its prior exact owner
        # receipt lives here rather than in the mutable current fields.
        prior = incoming_state.get("superseded_lifecycle") if isinstance(incoming_state, dict) else None
        if isinstance(prior, dict) and isinstance(prior.get("authorization"), dict):
            prior_owner = {
                "session_id": prior.get("session_id"), "source_path": prior.get("source_path"),
                "cwd": prior.get("cwd"), "window_generation": prior.get("window_generation"),
                "rotation": {"generation": prior.get("rotation_generation"), "request": None, "clear": None},
                "authorization": prior["authorization"],
            }
            consider(self, prior_owner)
        for owner, owner_state in self._owner_states():
            consider(owner, owner_state)
        if not owners:
            return None
        root_latest = (owners[0][1]["root_instruction_locator"], owners[0][1]["latest_instruction_locator"])
        if any((authorization["root_instruction_locator"], authorization["latest_instruction_locator"])
               != root_latest for _, authorization, _ in owners[1:]):
            raise ContextRuntimeError("恢复来源存在相互冲突的已验证授权 lineage")
        return min(owners, key=lambda owner: owner[0])[1:]

    def _inherit_registered_catalogue(self, candidate, source, *, incoming_state=None, include_registered=False):
        """Preserve registered history references without adopting an old owner's authority."""
        target = candidate["session_id"], str(source.path)
        local = self._catalogue_entries(self) if include_registered else []
        seen = {(entry["session_id"], entry["source_path"]) for entry in local}
        targets, references = seen | {target}, list(local)
        for owner, owner_state in self._owner_states():
            if not _owner_workspace_matches(owner_state, candidate["cwd"]):
                continue
            try:
                entries = self._catalogue_entries(owner)
            except (OSError, TypeError, ValueError, KeyError):
                continue  # An unreadable registry supplies no provenance.
            generations = [entry["generation"] for entry in entries
                           if (entry["session_id"], entry["source_path"]) in targets]
            if not generations:
                continue
            through = max(generations)
            for entry in entries:
                key = entry["session_id"], entry["source_path"]
                if entry["generation"] <= through and key not in seen:
                    seen.add(key)
                    references.append(entry)
        if len(references) > len(local):
            self._adopt_lineage_catalogue(candidate, candidate if incoming_state is None else incoming_state,
                                          source, references)

    def _adopt_lineage_catalogue(self, candidate, incoming_state, source, references):
        """Copy validated source references into this context without touching another owner's registry."""
        entries = self._catalogue_entries(self)
        floor = self._catalogue_floor(candidate, entries)
        source_path = str(source.path)
        target = (candidate["session_id"], source_path)
        unique = []
        seen = set()
        for reference in references:
            key = (reference["session_id"], reference["source_path"])
            if key not in seen:
                seen.add(key)
                unique.append(reference)
        if target not in seen:
            raise ContextRuntimeError("恢复 lineage 缺少当前来源引用")
        local = {}
        for entry in entries:
            local.setdefault((entry["session_id"], entry["source_path"]), []).append(entry["generation"])
        for reference in unique:
            key = (reference["session_id"], reference["source_path"])
            if key == target or key in local:
                continue
            floor += 1
            probe = {"session_id": reference["session_id"], "source_path": reference["source_path"],
                     "window_generation": floor}
            self._catalogue_source(probe)
            local[key] = [floor]
        target_generations = local.get(target, [])
        incoming_bound = self._same_resume_binding(incoming_state, *target, candidate["cwd"])
        if incoming_bound and any(generation >= floor for generation in target_generations):
            target_generation = max(generation for generation in target_generations if generation >= floor)
        else:
            floor += 1
            target_generation = floor
            self._catalogue_source({"session_id": target[0], "source_path": target[1],
                                    "window_generation": target_generation})
        candidate["window_generation"] = target_generation
        candidate["rotation"]["generation"] = max(candidate["rotation"]["generation"], floor, target_generation)

    @staticmethod
    def _resume_binding(event, sid, cwd):
        """Persist only the exact SessionStart identity needed for re-observation."""
        path = event.get("transcript_path")
        if not isinstance(path, str):
            raise ContextRuntimeError("SessionStart 缺少准确原生历史路径")
        return {"hook_event_name": "SessionStart", "source": "resume", "session_id": sid,
                "cwd": cwd, "transcript_path": path}

    def _registered_workspace(self, sid, source_path, cwd):
        """Prove workspace membership from exact verified catalogue registrations.

        Authorization snapshots play no part: a closed owner's latest locator
        naturally ages once genuine work is appended later.  Provisional current
        fields are never proof, while any owner registering or bound to this
        exact source in another workspace is conflicting evidence.
        """
        proved = False
        for owner, owner_state in self._owner_states():
            current = owner_state.get("session_id") == sid and owner_state.get("source_path") == source_path
            try:
                registered = any((entry["session_id"], entry["source_path"]) == (sid, source_path)
                                 for entry in self._catalogue_entries(owner))
            except (OSError, TypeError, ValueError, KeyError):
                registered = False  # An unreadable registry is neither proof nor conflict.
            if not (current or registered):
                continue
            if not _owner_workspace_matches(owner_state, cwd):
                raise ContextRuntimeError("恢复来源与已登记 owner 的工作目录不符")
            proved = proved or registered
        return proved

    def _verify_resume_workspace(self, binding, source, latest):
        """Keep an unverified source pending even when its prefix arrives later."""
        if binding.get("workspace_verified"):
            return
        # A registered owner may have legitimately changed cwd after launch.  Its
        # exact verified registration proves the workspace; its authorization
        # snapshot is not consulted and is never copied.
        if latest["cwd"] != binding["cwd"] and not self._registered_workspace(
                binding["session_id"], binding["transcript_path"], binding["cwd"]):
            raise ContextRuntimeError("恢复来源的实际模型用量工作目录不符")
        binding["workspace_verified"] = True

    def _complete_resume_binding(self, candidate, binding, *, incoming_state=None):
        """Complete a previously accepted resume only from a readable exact prefix."""
        sid = binding["session_id"]
        source = _source(binding["transcript_path"], sid)
        records = source._records(defer_incomplete_tail=True)
        latest = source._usage_from_records(records)
        self._verify_resume_workspace(binding, source, latest)
        candidate["source_path"] = str(source.path)
        self._catalogue_source(candidate)
        self._observe_authorization_records(candidate, source, records)
        activity = source._activity_from_records(records)
        # Calls already present in the resumed transcript belong to the prior
        # execution epoch.  Preserve their exact IDs for diagnostics, but do not
        # let them reintroduce an old deadlock or replay them.  New current-epoch
        # calls are tracked by subsequent real hooks in ``pending_tool_ids``.
        if "resume_historical_pending_tool_ids" not in candidate:
            candidate["resume_historical_pending_tool_ids"] = sorted(activity["pending_tools"])
            candidate["active_child_handles"] = sorted(activity["background_handles"])
        bounds = source._bounds_from_records(records)
        self._window(candidate, binding)
        observed = self._usage(candidate, True, sample=latest)
        if observed is None:
            raise ContextRuntimeError("恢复来源没有可用的实际原生用量")
        usage, _ = observed
        candidate["native_session_model"] = usage["actual_model"]
        owner_state = candidate if incoming_state is None else incoming_state
        if bounds["first"] is None:
            selected = self._resolve_resume_authorization(owner_state, source, sid, candidate["cwd"])
            if selected is None:
                self._wait(candidate, "native_root_authorization_pending")
                return False
            authorization, references = selected
            candidate["authorization"] = deepcopy(authorization)
            self._verify_authorization(candidate)
            self._adopt_lineage_catalogue(candidate, owner_state, source, references)
        else:
            self._inherit_registered_catalogue(candidate, source, incoming_state=owner_state)
        self._verify_authorization(candidate)
        if not binding.get("activity_observed"):
            candidate["resume_safe_boundary"] = True
            candidate["resume_snapshot"] = {
                "instruction_head": source._bounds_from_records(records)["last"],
                "usage_locator": usage["usage_locator"],
            }
        candidate["native_resume_confirmations"] = candidate.get("native_resume_confirmations", 0) + 1
        return True

    def _bind_resume(self, state, event, sid, *, incoming_state=None, reobserve=False):
        """Accept a validated native epoch before waiting for its history prefix."""
        incoming_state = state if incoming_state is None else incoming_state
        binding = self._resume_binding(event, sid, state["cwd"])
        known = any((entry["session_id"], entry["source_path"]) == (sid, binding["transcript_path"])
                    for entry in self._catalogue_entries(self))
        pending = incoming_state.get("pending_session_start")
        if (isinstance(pending, dict) and pending.get("session_id") == sid
                and pending.get("transcript_path") == binding["transcript_path"]):
            known = bool(pending.get("workspace_verified"))
            if reobserve and (pending.get("activity_observed") or pending.get("event_stamp")
                              != _history_stamp(binding["transcript_path"])):
                # A hook ran or native history changed since the recorded
                # SessionStart: that idle resume boundary is stale.
                binding["activity_observed"] = True
        binding["workspace_verified"] = known
        if not known and Path(binding["transcript_path"]).is_file():
            try:
                source = _source(binding["transcript_path"], sid)
                latest = source._usage_from_records(source._records(defer_incomplete_tail=True))
            except (OSError, TypeError, ValueError, HistoryError, ContextRuntimeError):
                pass  # The pending binding retains its workspace verification obligation.
            else:
                self._verify_resume_workspace(binding, source, latest)
        candidate = self._resume_candidate(state, incoming_state, sid, binding["transcript_path"])
        candidate["pending_session_start"] = binding
        self._catalogue_source(candidate)
        self._window(candidate, event)
        if event.get("model") is not None:
            candidate["native_session_model"] = event["model"]
        try:
            complete = self._complete_resume_binding(candidate, binding, incoming_state=incoming_state)
        except (OSError, TypeError, ValueError, HistoryError, ContextRuntimeError) as exc:
            self._wait(candidate, "resume_history_prefix_pending")
            candidate["diagnostic"] = redact(str(exc), core.secret_values())[:400]
        else:
            if complete:
                candidate.pop("pending_session_start", None)
                self._clear_wait(candidate)
        state.clear()
        state.update(candidate)

    def _retain_unbound_resume(self, state, binding, exc, *, reobserve=False):
        """Keep the prior bound epoch and record one validated resume event for exact reobservation."""
        failure = redact(str(exc), core.secret_values())[:400] or type(exc).__name__
        prior = self._unbound_resume(state)
        same = prior is not None and all(prior.get(key) == binding[key] for key in _RESUME_IDENTITY)
        # Only reobservation continues the same native epoch; a repeated genuine
        # SessionStart is a new idle epoch of that session.
        epoch = same and reobserve
        stamp = _history_stamp(binding["transcript_path"])
        record = {key: binding[key] for key in ("hook_event_name", "source", *_RESUME_IDENTITY)}
        record.update(bound=False, workspace_verified=False,
                      first_failure=(prior.get("first_failure") or failure) if same else failure,
                      last_failure=failure, history_stamp=stamp,
                      event_stamp=prior.get("event_stamp") if epoch else stamp)
        if epoch and prior.get("activity_observed"):
            record["activity_observed"] = True
        state["pending_session_start"] = record
        state["native_session_uncertain"] = True
        state["launch_session_ids"] = sorted(set(state.get("launch_session_ids", [])) | {binding["session_id"]})
        self._wait(state, UNBOUND_RESUME_WAIT)
        state["diagnostic"] = record["first_failure"]

    def _reobserve_unbound_resume(self, state, binding, *, force=False):
        """Re-verify only the exactly recorded SessionStart identity; never a later hook identity."""
        if not force and binding.get("history_stamp") == _history_stamp(binding["transcript_path"]):
            return False  # No new native fact; tui-recover or a target hook forces one exact retry.
        event = {key: binding[key] for key in ("hook_event_name", "source", *_RESUME_IDENTITY)}
        observed = self._session_start_transition(state, event, reobserve=True)
        state.clear()
        state.update(observed)
        return state.get("pending_session_start") is None and state.get("session_id") == binding["session_id"]

    def _reconcile_pending_session_start(self, state, *, force=False):
        binding = state.get("pending_session_start")
        if not isinstance(binding, dict) or binding.get("source") != "resume":
            return False
        if binding.get("bound", True) is False:
            return self._reobserve_unbound_resume(state, binding, force=force)
        if (binding.get("session_id") != state.get("session_id")
                or binding.get("transcript_path") != state.get("source_path")):
            self._wait(state, "resume_binding_identity_drift")
            return False
        try:
            complete = self._complete_resume_binding(state, binding, incoming_state=state)
        except (OSError, TypeError, ValueError, HistoryError, ContextRuntimeError) as exc:
            self._wait(state, "resume_history_prefix_pending")
            state["diagnostic"] = redact(str(exc), core.secret_values())[:400]
            return False
        if complete:
            state.pop("pending_session_start", None)
            self._clear_wait(state)
            return True
        return False

    def _observe_resume_target(self, state, event):
        """Mark genuine activity on a pending resume target and reobserve only its exact identity.

        Returns True when the hook belongs to a still-unadopted resume target
        and must not be applied to the prior epoch.
        """
        pending = state.get("pending_session_start")
        if (not isinstance(pending, dict) or event.get("hook_event_name") not in _MAIN_HOOKS
                or event.get("agent_id")
                or event.get("session_id") != pending.get("session_id")):
            return False
        pending["activity_observed"] = True
        if pending.get("bound", True) is not False or event.get("transcript_path") != pending.get("transcript_path"):
            return False
        self._reconcile_pending_session_start(state, force=True)
        held = self._unbound_resume(state)
        if held is None or event.get("session_id") == state.get("session_id"):
            return False
        self._wait(state, UNBOUND_RESUME_WAIT)
        state["diagnostic"] = held["first_failure"]
        return True

    def _resume_flushed(self, state, source, records):
        """A fresh resumed turn invalidates only the old boundary, never observation."""
        snapshot = state.get("resume_snapshot")
        if not isinstance(snapshot, dict):
            self._invalidate_resume_boundary(state, "resume_boundary_snapshot_missing")
            self._wait(state, "resume_boundary_stale_waiting_for_next_boundary")
            return False
        if source._bounds_from_records(records)["last"] != snapshot.get("instruction_head"):
            self._invalidate_resume_boundary(state, "fresh_user_activity_after_resume")
            self._wait(state, "resume_boundary_stale_waiting_for_next_boundary")
            return False
        if source._usage_from_records(records)["locator"] != snapshot.get("usage_locator"):
            self._invalidate_resume_boundary(state, "fresh_model_activity_after_resume")
            self._wait(state, "resume_boundary_stale_waiting_for_next_boundary")
            return False
        return True
