"""Continue a recorded local run using constrained relation indices, without gold labels."""
import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from plantkg.core.hashing import hash_file
from urllib.request import Request, urlopen

from .evaluation import RELATION_LABELS, stable_split, validate_maintie_prediction
from .maintie_local import GOLD_SHA256


def relation_schema(count):
    relation = {'type': 'object', 'required': ['head', 'tail', 'type'], 'properties': {
        'head': {'type': 'integer', 'minimum': 0, 'maximum': max(0, count - 1)},
        'tail': {'type': 'integer', 'minimum': 0, 'maximum': max(0, count - 1)},
        'type': {'type': 'string', 'enum': list(RELATION_LABELS)}}}
    array = {'type': 'array', 'items': relation}
    if not count:
        array['maxItems'] = 0
    return {'type': 'object', 'required': ['relations'], 'properties': {'relations': array}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('input'); p.add_argument('previous'); p.add_argument('--output', required=True)
    args = p.parse_args()
    source = Path(args.input).read_bytes()
    if hashlib.sha256(source).hexdigest() != GOLD_SHA256:
        raise ValueError('input does not match pinned MaintIE SHA-256')
    previous = Path(args.previous)
    meta_bytes = (previous / 'metadata.json').read_bytes()
    raw_bytes = (previous / 'responses.json').read_bytes()
    meta = json.loads(meta_bytes); raw = json.loads(raw_bytes); records = json.loads(source)
    if meta.get('source_sha256') != GOLD_SHA256:
        raise ValueError('previous run used different input')
    previous_digest = meta.get('model_digest')
    if not isinstance(previous_digest, str) or not previous_digest.strip():
        raise ValueError('previous run lacks model_digest; establish a verifiable model identity before repair')
    try:
        with urlopen('http://127.0.0.1:11434/api/tags', timeout=10) as response:
            models = json.load(response)['models']
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError('cannot verify model digest with local Ollama') from exc
    model_info = next((model for model in models if model.get('name') == meta['model']), None)
    current_digest = model_info.get('digest') if model_info else None
    if not isinstance(current_digest, str) or not current_digest.strip():
        raise ValueError('local Ollama model is missing or has no verifiable digest')
    if current_digest != previous_digest:
        raise ValueError('model digest mismatch between previous run and local Ollama')
    out = Path(args.output).resolve(); repo = Path(__file__).resolve().parents[3]
    if out == repo or repo in out.parents:
        raise ValueError('raw responses must be outside the repository')
    out.mkdir(parents=True, exist_ok=False)
    predictions, repairs, failures = {}, {}, {}
    started = datetime.now(timezone.utc).isoformat(); t0 = time.monotonic()
    request_count = 0
    for index, item in enumerate(records):
        if stable_split(index, meta['seed']) != 'test':
            continue
        key = str(index); attempts = raw.get(key, [])
        if not isinstance(attempts, list):
            raise ValueError('previous run must record attempts as arrays')
        entity_only = None
        for reply in reversed(attempts):
            try:
                value = json.loads(reply['response'])
                value.update(uncertainty=1, impact=1)
                if entity_only is None:
                    candidate = {**value, 'relations': []}
                    validate_maintie_prediction({'tokens': item['tokens']}, candidate, key)
                    entity_only = candidate
                validate_maintie_prediction({'tokens': item['tokens']}, value, key)
                predictions[key] = value
                break
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        if key in predictions:
            continue
        if entity_only is None:
            failures[key] = 'no valid predicted entities to repair'
            continue
        prompt = ('Predict directed relations between the listed predicted entities. '
                  'head and tail must index the entity array from zero. Do not add entities. '
                  'Return JSON with a relations array. Tokens: ' + json.dumps(item['tokens'], ensure_ascii=False) +
                  '\nPredicted entities: ' + json.dumps(entity_only['entities'], ensure_ascii=False))
        body = json.dumps({'model': meta['model'], 'prompt': prompt, 'format': relation_schema(len(entity_only['entities'])),
                           'stream': False, 'think': False,
                           'options': {'temperature': 0, 'seed': meta['seed'], 'num_predict': 1024}}).encode()
        try:
            request_count += 1
            request = Request('http://127.0.0.1:11434/api/generate', data=body, headers={'Content-Type': 'application/json'})
            with urlopen(request, timeout=180) as response:
                reply = json.load(response)
            repairs[key] = reply
            value = {**entity_only, 'relations': json.loads(reply['response'])['relations']}
            validate_maintie_prediction({'tokens': item['tokens']}, value, key)
            predictions[key] = value
        except (ValueError, KeyError, TypeError, OSError) as exc:
            failures[key] = str(exc)
    (out / 'repair-responses.json').write_text(json.dumps(repairs, ensure_ascii=False, indent=2) + '\n', encoding='utf8', newline='\n')
    (out / 'failures.json').write_text(json.dumps(failures, ensure_ascii=False, indent=2) + '\n', encoding='utf8', newline='\n')
    pred_path = out / 'predictions.json'
    if not failures:
        pred_path.write_text(json.dumps(predictions, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf8', newline='\n')
    result = {'source_kind': 'local_model_adaptive_repair', 'model': meta['model'], 'seed': meta['seed'],
              'previous_model_digest': previous_digest, 'repair_model_digest': current_digest,
              'model_digest_verified': True,
              'source_sha256': hash_file(args.input), 'hash_scheme': 'utf8-lf-v1',
              'previous_metadata_sha256': hashlib.sha256(meta_bytes).hexdigest(),
              'previous_responses_sha256': hashlib.sha256(raw_bytes).hexdigest(), 'started_utc': started,
              'previous_elapsed_seconds': meta['elapsed_seconds'], 'repair_elapsed_seconds': time.monotonic() - t0,
              'previous_request_count': meta.get('request_count'), 'repair_request_count': request_count,
              'repaired_count': len(repairs), 'valid_count': len(predictions), 'failed_count': len(failures),
              'prediction_sha256': hash_file(pred_path) if not failures else None,
              'api_fee': 0, 'api_fee_reason': 'local endpoint; machine and human costs excluded'}
    if result['previous_request_count'] is None:
        result['previous_request_count_reason'] = 'previous run did not record attempted request count'
    (out / 'metadata.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf8', newline='\n')
    print(json.dumps(result, indent=2))
    if failures:
        raise SystemExit('Incomplete predictions; see failures.json')


if __name__ == '__main__':
    main()
