"""Incremental native activity state, shared by full and retained observations."""
from __future__ import annotations

import hashlib
from typing import Any

from .history import _blocks, _content, _json, _results, _task_notice, terminal_task_result


class NativeActivity:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}
        self.results: set[str] = set()
        self.backgrounds: dict[str, Any] = {}
        self.controls: dict[str, Any] = {}
        self.terminal_backgrounds: set[str] = set()
        self.lifecycle: dict[str, Any] = {}

    def extend(self, records) -> None:
        tools, results, backgrounds, controls = self.tools, self.results, self.backgrounds, self.controls
        terminal_backgrounds, lifecycle = self.terminal_backgrounds, self.lifecycle

        def evidence(handle):
            return lifecycle.setdefault(handle, {'launch_tool_ids': set(), 'terminal_tool_ids': set()})

        for info in records:
            notice = _task_notice(info.data)
            if notice is not None:
                handle, _, launch_id = notice
                proof, launched = evidence(handle), backgrounds.get(handle)
                if launch_id is not None:
                    proof['terminal_tool_ids'].add(launch_id)
                if launched is not None and (
                        launch_id in launched['tool_use_ids']
                        or launch_id is None and len(proof['launch_tool_ids']) == 1):
                    proof['terminal_tool_ids'].update(launched['tool_use_ids'])
                    backgrounds.pop(handle)
                    terminal_backgrounds.add(handle)
            if info.kind == 'assistant':
                for block in _blocks(_content(info.data)):
                    if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('id'):
                        tools[block['id']] = {'name': block.get('name'),
                                              'input_hash': hashlib.sha256(_json(block.get('input', {})).encode()).hexdigest()}
                        request = block.get('input', {})
                        if block.get('name') in {'TaskStop', 'TaskOutput'} and isinstance(request, dict):
                            target = request.get('task_id')
                            if isinstance(target, str) and target:
                                controls[block['id']] = (block['name'], target, backgrounds.get(target))
            if info.kind != 'tool_result':
                continue
            ids = {block['tool_use_id'] for block in _results(info.data)}
            new_ids = ids - results
            results.update(ids)
            response = info.data.get('toolUseResult')
            if not new_ids or not isinstance(response, dict):
                continue
            for key in ('backgroundTaskId', 'resumedAgentId'):
                handle = response.get(key)
                if isinstance(handle, str) and handle:
                    backgrounds[handle] = {'tool_use_ids': sorted(new_ids), 'source_record': info.message_id}
                    terminal_backgrounds.discard(handle)
                    evidence(handle)['launch_tool_ids'].update(new_ids & tools.keys())
            handle = response.get('agentId')
            if response.get('isAsync') is True and isinstance(handle, str) and handle:
                backgrounds[handle] = {'tool_use_ids': sorted(new_ids), 'source_record': info.message_id}
                terminal_backgrounds.discard(handle)
                evidence(handle)['launch_tool_ids'].update(new_ids & tools.keys())
            if len(ids) == 1 and new_ids and not any(block.get('is_error', False) for block in _results(info.data)):
                control = controls.get(next(iter(ids)))
                if control is not None:
                    name, target, launched = control
                    settled = terminal_task_result(name, {'task_id': target}, response)
                    if settled and launched is not None and backgrounds.get(settled) == launched:
                        backgrounds.pop(settled, None)
                        terminal_backgrounds.add(settled)
                        evidence(settled)['terminal_tool_ids'].update(launched['tool_use_ids'])

    def snapshot(self) -> dict[str, Any]:
        from copy import deepcopy
        return {'pending_tools': deepcopy({key: value for key, value in self.tools.items() if key not in self.results}),
                'background_handles': deepcopy(self.backgrounds),
                'settled_tool_ids': sorted(self.tools.keys() & self.results),
                'terminal_background_handles': sorted(self.terminal_backgrounds),
                'background_lifecycle': {handle: {key: sorted(ids) for key, ids in proof.items()}
                                         for handle, proof in self.lifecycle.items()}}
