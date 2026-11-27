"""Content-addressed, plant-scoped integration artifacts for B04."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path

import pyarrow.parquet as pq

from plantkg.core.contracts import ComponentRef, ErrorCode, PlantkgError
from plantkg.core.versions import canonical
from plantkg.storage.integrity import check_references


def _fail(message):
    raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, message)


class IntegrationArtifacts:
    """Immutable JSON artifacts registered in the existing B03 catalog."""

    def __init__(self, writer, series, plant_id, path):
        self.writer, self.series, self.plant_id = writer, series, plant_id
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)

    def _put(self, kind, body):
        self.writer.require_outside_transaction()
        raw = canonical(body).encode('utf-8')
        sha = hashlib.sha256(raw).hexdigest()
        ref = f'{kind}:v1:{sha}'
        final = self.path / (kind + '-' + sha + '.json')
        fd, name = tempfile.mkstemp(prefix='.artifact-', dir=self.path)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(name, final)
            except FileExistsError:
                if final.read_bytes() != raw:
                    _fail('Existing artifact content differs')
            def register(con):
                con.execute('INSERT OR IGNORE INTO storage_artifacts VALUES (?,?,?,?,?,?,?)',
                            (self.plant_id, kind, sha, ref, sha, 1, None))
            self.writer.submit(register)
        finally:
            Path(name).unlink(missing_ok=True)
        result = ComponentRef(ref=ref, content_hash='sha256:' + sha)
        self._read(result, kind)
        return result

    def _read(self, component, kind):
        match = re.fullmatch(re.escape(kind) + r':v1:([0-9a-f]{64})', component.ref)
        if not match or component.content_hash != 'sha256:' + match.group(1):
            _fail('Invalid artifact reference or hash')
        sha = match.group(1)
        with self.writer.read_only() as con:
            row = con.execute('SELECT checksum,format_version FROM storage_artifacts WHERE plant_id=? AND kind=? AND reference=?',
                              (self.plant_id, kind, component.ref)).fetchone()
        if row != (sha, 1):
            _fail('Artifact not registered for this plant')
        try:
            raw = (self.path / (kind + '-' + sha + '.json')).read_bytes()
            if hashlib.sha256(raw).hexdigest() != sha:
                _fail('Artifact checksum mismatch')
            body = json.loads(raw)
            if canonical(body).encode('utf-8') != raw or body.get('plant_id') != self.plant_id or body.get('format_version') != 1:
                _fail('Invalid artifact content')
            return body
        except (OSError, ValueError, TypeError, AttributeError):
            _fail('Artifact missing or invalid')

    def _check_entry(self, entry):
        if not isinstance(entry, dict) or set(entry) != {'quantity', 'ref', 'source_id', 'source_version', 'basis'}:
            _fail('Invalid source mapping entry')
        if any(not isinstance(entry[k], str) or not entry[k] for k in entry):
            _fail('Invalid source mapping values')
        ref = entry['ref']
        if not re.fullmatch(r'series:v1:[0-9a-f]{64}', ref):
            _fail('Unsupported series reference')
        if check_references(self.writer, None, self.series, self.plant_id, [ref])[0].status != 'complete':
            _fail('Series partition missing, corrupt or unregistered')
        try:
            table = pq.read_table(self.series.path / (ref.split(':')[-1] + '.parquet'), columns=['quantity'])
            metadata = json.loads(table.schema.metadata[b'plantkg_bundle'])
            if (metadata['source_id'], metadata['source_version']) != (entry['source_id'], entry['source_version']):
                _fail('Series source identity mismatch')
            if entry['quantity'] not in table.column('quantity').to_pylist():
                _fail('Quantity absent from partition')
        except (OSError, ValueError, KeyError, TypeError):
            _fail('Series metadata invalid')

    def prepare_series_map(self, entries):
        entries = [dict(e) for e in entries]
        if not entries or len({e.get('quantity') for e in entries}) != len(entries):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, 'Source map quantities must be unique and nonempty')
        for entry in entries:
            self._check_entry(entry)
        return self._put('series-map', {'format_version': 1, 'plant_id': self.plant_id,
                                        'entries': sorted(entries, key=lambda e: e['quantity'])})

    def read_series_map(self, component):
        body = self._read(component, 'series-map')
        entries = body.get('entries')
        if not isinstance(entries, list) or not entries or len({e.get('quantity') for e in entries if isinstance(e, dict)}) != len(entries):
            _fail('Invalid source map')
        for entry in entries:
            self._check_entry(entry)
        return {e['quantity']: e['ref'] for e in entries}

    @staticmethod
    def _validate_parameters(body):
        for key in ('model_version', 'parameter_version', 'configuration_ref'):
            if not isinstance(body.get(key), str) or not body[key]:
                raise PlantkgError(ErrorCode.INVALID_REQUEST, 'Missing parameter metadata')
        for key in ('pv_correction_k', 'pv_capacity_kw'):
            value = body.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise PlantkgError(ErrorCode.INVALID_REQUEST, 'Parameter must be a finite number')
        if body['pv_correction_k'] < 0 or body['pv_capacity_kw'] <= 0:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, 'Invalid correction or capacity')

    def prepare_parameters(self, *, pv_capacity_kw, model_version, parameter_version, configuration_ref, pv_correction_k=1):
        body = dict(format_version=1, plant_id=self.plant_id, pv_capacity_kw=pv_capacity_kw,
                    model_version=model_version, parameter_version=parameter_version,
                    configuration_ref=configuration_ref, pv_correction_k=pv_correction_k)
        self._validate_parameters(body)
        return self._put('parameters', body)

    def read_parameters(self, component):
        body = self._read(component, 'parameters')
        self._validate_parameters(body)
        return body

    def verify_map(self, component):
        try:
            self.read_series_map(component)
            return 'complete'
        except (PlantkgError, ValueError, TypeError):
            return 'corrupt'

    def verify_parameters(self, component):
        try:
            self.read_parameters(component)
            return 'complete'
        except (PlantkgError, ValueError, TypeError):
            return 'corrupt'

    def parameters_for_run(self, context, versions, runs):
        if context.plant_id != self.plant_id or versions.plant_id != self.plant_id or runs.plant_id != self.plant_id:
            raise PlantkgError(ErrorCode.CROSS_PLANT, 'Plant mismatch')
        if not context.run_id or not context.version_manifest_id:
            raise PlantkgError(ErrorCode.PRECONDITION, 'Run and fixed manifest required')
        run = runs.get_run(context.run_id)
        if run.version_manifest_id != context.version_manifest_id:
            raise PlantkgError(ErrorCode.PRECONDITION, 'Run manifest mismatch')
        manifest = versions.usable(context.version_manifest_id)
        component = manifest.components.get('parameters')
        if component is None or not component.ref.startswith('parameters:v1:'):
            raise PlantkgError(ErrorCode.PRECONDITION, 'Frozen parameters unavailable')
        return self.read_parameters(component) | {'manifest_id': manifest.version_manifest_id, 'parameter_ref': component.ref}
