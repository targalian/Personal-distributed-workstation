"""Bind every acceptance criterion to immutable sources without treating self-review as tests."""

import hashlib
import json
from typing import Any

from .component_service import ComponentService
from .runtime_artifacts import RuntimeArtifacts, artifact_root


class AcceptanceEvidenceService(ComponentService):
    """Read only local scoped evidence; remote or absent records stay unverified."""

    OPERATIONS = ('catalog', 'bind')

    def catalog(self, task: dict, outputs: dict) -> list[dict]:
        found = []
        for value in outputs.values():
            if not isinstance(value, dict):
                continue
            output = value.get('output', value)
            if not isinstance(output, dict):
                continue
            execution = output.get('execution', {})
            scope = execution.get('scope', {})
            if (scope.get('task_id') != task.get('task_id', '') or
                    scope.get('project_id') != task.get('project_id', '')):
                continue
            for step in output.get('steps', [])[:200]:
                ref = step.get('artifact', {})
                try:
                    store = RuntimeArtifacts(artifact_root(self.owner.shared_folder),
                                             execution['execution_id'], scope)
                    # Read verifies the checksum before trusting any preview.
                    page = store.read(ref['artifact_id'], limit=1200, view='observation')
                except (OSError, ValueError, KeyError, TypeError):
                    continue
                if page['kind'] != 'tool_result':
                    continue
                found.append(dict(id=ref['artifact_id'], kind='tool_observation',
                                  execution_id=execution['execution_id'], scope=scope,
                                  sha256=page['sha256'], tool=step.get('tool', ''),
                                  preview=page['content'], checksum_verified=True))
                if len(found) >= 200:
                    return found
        return found

    def bind(self, task: dict, criteria: list[str], deliverable: str, review: dict,
             catalog: list[dict]) -> dict:
        """Persist exactly what the reviewer saw and attach references to every criterion."""
        digest = hashlib.sha256(deliverable.encode('utf-8')).hexdigest()
        source: dict[str, Any] = dict(kind='deliverable_snapshot', sha256=digest,
                                      content=deliverable, source='model_or_aggregation')
        try:
            scope = dict(agent_id=self.owner.agent_id, task_id=str(task.get('task_id', '')),
                         project_id=str(task.get('project_id', '')), attempt_id=str(task.get('attempt_id', '')))
            store = RuntimeArtifacts.create(self.owner.shared_folder, scope)
            source = {**store.save('acceptance_source', dict(observation=deliverable)), 'scope': scope,
                      'source': 'model_or_aggregation'}
            store.update('completed', phase='acceptance_source', protected_evidence=True)
        except (OSError, ValueError, AttributeError):
            pass  # Inline immutable copy travels in the persisted delivery as a compatibility fallback.
        indexed = {item['id']: item for item in catalog}
        checks = []
        for index, criterion in enumerate(criteria):
            old = review.get('checks', [])
            item = old[index] if index < len(old) else {}
            references = [indexed[key] for key in item.get('evidence_ids', []) if key in indexed]
            for ref in references:
                self._protect_evidence(ref)
            checks.append({**item, 'criterion': criterion, 'met': item.get('met') is True,
                           'assessment_kind': 'model_self_review', 'source_refs': [source, *references],
                           'evidence_verified': bool(references),
                           'verification_status': 'observations_linked' if references else 'unverified'})
        unmet = [item['criterion'] for item in checks if not item['met']]
        return {**review, 'checks': checks, 'reviewed': len(checks), 'unmet': unmet,
                'verdict': 'risk' if unmet else 'pass', 'assessment_kind': 'model_self_review',
                'independent_verification': 'not_performed', 'deliverable_sha256': digest,
                'evidence_catalog': catalog}

    def _protect_evidence(self, reference: dict) -> None:
        try:
            from .artifact_lock import artifact_guard
            from .runtime_artifacts import _encoded
            store = RuntimeArtifacts(artifact_root(self.owner.shared_folder),
                                     reference['execution_id'], reference['scope'])
            with artifact_guard(store.root, store.execution_id):
                run = store.inspect()
                run['protected_evidence'] = True
                store._atomic(store.directory / 'run.json', _encoded(run))
        except (KeyError, OSError, ValueError):
            pass
