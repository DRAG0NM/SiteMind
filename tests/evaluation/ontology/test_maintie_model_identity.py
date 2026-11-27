import hashlib
import io
import json
import sys
from urllib.error import URLError

import pytest

from plantkg.core.hashing import hash_file
from plantkg.evaluation.ontology import __main__ as scoring
from plantkg.evaluation.ontology import evaluation, maintie_local, maintie_repair


@pytest.fixture
def repair_run(tmp_path, monkeypatch):
    source = tmp_path / 'source.json'
    source.write_text(json.dumps([{'text': 'pump', 'tokens': ['pump'], 'entities': [], 'relations': []}]), encoding='utf8')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(maintie_repair, 'GOLD_SHA256', digest)
    monkeypatch.setattr(maintie_repair, 'stable_split', lambda *_: 'test')
    previous = tmp_path / 'previous'
    previous.mkdir()
    metadata = {'source_sha256': digest, 'model': 'model', 'model_digest': 'sha256:original',
                'seed': 42, 'elapsed_seconds': 2.0, 'request_count': 3}
    invalid = {'entities': [{'start': 0, 'end': 1, 'type': 'PhysicalObject'}],
               'relations': [{'head': 0, 'tail': 1, 'type': 'hasPart'}]}
    (previous / 'metadata.json').write_text(json.dumps(metadata), encoding='utf8')
    (previous / 'responses.json').write_text(json.dumps({'0': [{'response': json.dumps(invalid)}]}), encoding='utf8')
    output = tmp_path / 'output'
    monkeypatch.setattr(sys, 'argv', ['maintie_repair', str(source), str(previous), '--output', str(output)])
    return previous, output


def response(data):
    return io.BytesIO(json.dumps(data).encode('utf8'))


def test_matching_digest_permits_repair_and_records_provenance(repair_run, monkeypatch):
    previous, output = repair_run
    calls = []
    def fake_urlopen(request, timeout):
        calls.append(request if isinstance(request, str) else request.full_url)
        if isinstance(request, str):
            return response({'models': [{'name': 'model', 'digest': 'sha256:original'}]})
        return response({'response': json.dumps({'relations': []})})
    monkeypatch.setattr(maintie_repair, 'urlopen', fake_urlopen)
    maintie_repair.main()
    meta = json.loads((output / 'metadata.json').read_text(encoding='utf8'))
    assert calls == ['http://127.0.0.1:11434/api/tags', 'http://127.0.0.1:11434/api/generate']
    assert meta['previous_model_digest'] == meta['repair_model_digest'] == 'sha256:original'
    assert meta['model_digest_verified'] is True
    assert meta['previous_request_count'] == 3 and meta['repair_request_count'] == 1
    assert meta['previous_elapsed_seconds'] == 2.0 and meta['repair_elapsed_seconds'] >= 0
    assert meta['previous_metadata_sha256'] == hashlib.sha256((previous / 'metadata.json').read_bytes()).hexdigest()
    assert meta['previous_responses_sha256'] == hashlib.sha256((previous / 'responses.json').read_bytes()).hexdigest()
    assert meta['hash_scheme'] == 'utf8-lf-v1'
    assert meta['prediction_sha256'] == hash_file(output / 'predictions.json')


@pytest.mark.parametrize('condition', ['mismatch', 'missing', 'unavailable'])
def test_unverifiable_identity_stops_before_inference(repair_run, monkeypatch, condition):
    previous, output = repair_run
    if condition == 'missing':
        path = previous / 'metadata.json'
        meta = json.loads(path.read_text(encoding='utf8'))
        del meta['model_digest']
        path.write_text(json.dumps(meta), encoding='utf8')
    calls = []
    def fake_urlopen(request, timeout):
        calls.append(request if isinstance(request, str) else request.full_url)
        if not isinstance(request, str):
            pytest.fail('inference must not be called')
        if condition == 'unavailable':
            raise URLError('connection refused')
        return response({'models': [{'name': 'model', 'digest': 'sha256:changed'}]})
    monkeypatch.setattr(maintie_repair, 'urlopen', fake_urlopen)
    expected = {'mismatch': 'digest mismatch', 'missing': 'lacks model_digest',
                'unavailable': 'cannot verify model digest'}[condition]
    with pytest.raises((ValueError, RuntimeError), match=expected):
        maintie_repair.main()
    assert calls == ([] if condition == 'missing' else ['http://127.0.0.1:11434/api/tags'])
    assert not output.exists()


def test_previous_record_without_digest_remains_scoreable(tmp_path, monkeypatch):
    source = tmp_path / 'source.json'
    prediction = tmp_path / 'predictions.json'
    source.write_text(json.dumps([{'text': 'pump', 'tokens': ['pump'], 'entities': [], 'relations': []}]), encoding='utf8')
    prediction.write_text(json.dumps({'0': {'entities': [], 'relations': [], 'uncertainty': 1, 'impact': 1}}), encoding='utf8')
    metadata = tmp_path / 'metadata.json'
    metadata.write_text(json.dumps({'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                                    'prediction_sha256': hashlib.sha256(prediction.read_bytes()).hexdigest(),
                                    'seed': 42}), encoding='utf8')
    monkeypatch.setattr(evaluation, 'stable_split', lambda *_: 'test')
    output = tmp_path / 'score'
    monkeypatch.setattr(sys, 'argv', ['score', str(source), '--maintie-predictions', str(prediction),
                                    '--run-metadata', str(metadata), '--output', str(output)])
    scoring.main()
    result = json.loads((output / 'results.json').read_text(encoding='utf8'))
    assert result['items'] == 1 and 'model_digest' not in result['run_metadata']


def test_first_stage_records_attempted_requests(tmp_path, monkeypatch):
    source = tmp_path / 'source.json'
    source.write_text(json.dumps([{'tokens': ['pump']}]), encoding='utf8')
    monkeypatch.setattr(maintie_local, 'GOLD_SHA256', hashlib.sha256(source.read_bytes()).hexdigest())
    monkeypatch.setattr(maintie_local, 'stable_split', lambda *_: 'test')
    def fake_urlopen(request, timeout):
        if isinstance(request, str):
            return response({'models': [{'name': 'model', 'digest': 'sha256:original'}]})
        return response({'response': json.dumps({'entities': [], 'relations': []})})
    monkeypatch.setattr(maintie_local, 'urlopen', fake_urlopen)
    output = tmp_path / 'output'
    monkeypatch.setattr(sys, 'argv', ['maintie_local', str(source), '--model', 'model', '--output', str(output)])
    maintie_local.main()
    meta = json.loads((output / 'metadata.json').read_text(encoding='utf8'))
    assert meta['request_count'] == 1 and meta['model_digest'] == 'sha256:original'
    assert meta['hash_scheme'] == 'utf8-lf-v1'
    assert meta['prediction_sha256'] == hash_file(output / 'predictions.json')


def test_first_stage_rejects_missing_digest_before_inference(tmp_path, monkeypatch):
    source = tmp_path / 'source.json'
    source.write_text('[]', encoding='utf8')
    monkeypatch.setattr(maintie_local, 'GOLD_SHA256', hashlib.sha256(source.read_bytes()).hexdigest())
    calls = []
    def fake_urlopen(request, timeout):
        calls.append(request)
        return response({'models': [{'name': 'model'}]})
    monkeypatch.setattr(maintie_local, 'urlopen', fake_urlopen)
    output = tmp_path / 'output'
    monkeypatch.setattr(sys, 'argv', ['maintie_local', str(source), '--model', 'model', '--output', str(output)])
    with pytest.raises(ValueError, match='no verifiable digest'):
        maintie_local.main()
    assert calls == ['http://127.0.0.1:11434/api/tags'] and not output.exists()


def test_new_metadata_hash_is_portable_across_crlf_copy(tmp_path, monkeypatch):
    source = tmp_path / 'source.json'
    prediction = tmp_path / 'prediction.json'
    source.write_text(json.dumps([{'text': 'pump', 'tokens': ['pump'],
                                   'entities': [], 'relations': []}], indent=2) + '\n',
                      encoding='utf8', newline='\n')
    prediction.write_text(json.dumps({'0': {'entities': [], 'relations': [],
                                             'uncertainty': 1, 'impact': 1}}, indent=2) + '\n',
                          encoding='utf8', newline='\n')
    metadata = tmp_path / 'metadata.json'
    source_hash, prediction_hash = hash_file(source), hash_file(prediction)
    metadata.write_text(json.dumps({'hash_scheme': 'utf8-lf-v1', 'source_sha256': source_hash,
                                     'prediction_sha256': prediction_hash, 'seed': 42}),
                        encoding='utf8')
    for path in (source, prediction):
        path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
    assert hash_file(source) == source_hash
    assert hash_file(prediction) == prediction_hash
    monkeypatch.setattr(evaluation, 'stable_split', lambda *_: 'test')
    output = tmp_path / 'score'
    monkeypatch.setattr(sys, 'argv', ['score', str(source), '--maintie-predictions', str(prediction),
                                     '--run-metadata', str(metadata), '--output', str(output)])
    scoring.main()
    result = json.loads((output / 'results.json').read_text(encoding='utf8'))
    assert result['items'] == 1
    assert result['source_sha256'] == source_hash
    assert result['predictions_sha256'] == prediction_hash


def test_first_stage_ollama_unavailable_is_readable_and_does_not_create_output(tmp_path, monkeypatch):
    source = tmp_path / 'source.json'
    source.write_text('[]', encoding='utf8')
    monkeypatch.setattr(maintie_local, 'GOLD_SHA256', hashlib.sha256(source.read_bytes()).hexdigest())

    def unavailable(*args, **kwargs):
        raise URLError('connection refused')

    monkeypatch.setattr(maintie_local, 'urlopen', unavailable)
    output = tmp_path / 'output'
    monkeypatch.setattr(sys, 'argv', ['maintie_local', str(source), '--model', 'model',
                                     '--output', str(output)])
    with pytest.raises(RuntimeError, match='cannot verify model digest with local Ollama'):
        maintie_local.main()
    assert not output.exists()
