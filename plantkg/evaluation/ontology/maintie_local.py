"""Generate frozen MaintIE predictions with a local Ollama model, without gold labels.

The model receives only indexed tokens and public top-level labels. Failed or
invalid responses are retained separately and prevent creation of a complete
prediction file; they are never converted to empty predictions.
"""
import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from plantkg.core.hashing import hash_file
from urllib.request import Request, urlopen

from .evaluation import (ENTITY_LABELS, RELATION_LABELS, stable_split,
                         validate_maintie_prediction)

GOLD_SHA256 = 'ef811cf4ffd0c90e6a13b1369e24e9d1d08f9a294d873ab4fb1a580096d7d6e0'


def prompt_for(tokens):
    return ('Extract entities and directed relations from these indexed tokens. '
            'Use zero-based token indices and exclusive end indices, never character offsets. '
            'Each entity has start, end and type; each relation has head, tail (indices into '
            'the entities list) and type. Return JSON with arrays entities and relations. '
            f'Entity types: {", ".join(ENTITY_LABELS)}. '
            f'Relation types: {", ".join(RELATION_LABELS)}. '
            'Tokens: ' + json.dumps(list(enumerate(tokens)), ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', help='Pinned gold file; only tokens are sent to the model')
    parser.add_argument('--model', required=True)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--retries', type=int, default=2, help='Retries after an invalid response')
    parser.add_argument('--output', required=True, help='New directory outside the repository')
    args = parser.parse_args()
    if args.retries < 0:
        raise ValueError('retries must be non-negative')
    out = Path(args.output).resolve()
    repo = Path(__file__).resolve().parents[3]
    if out == repo or repo in out.parents:
        raise ValueError('Raw model responses must be stored outside the repository')
    source = Path(args.input).read_bytes()
    if hashlib.sha256(source).hexdigest() != GOLD_SHA256:
        raise ValueError('input does not match the pinned MaintIE gold SHA-256')
    try:
        with urlopen('http://127.0.0.1:11434/api/tags', timeout=10) as response:
            models = json.load(response)['models']
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError('cannot verify model digest with local Ollama') from exc
    model_info = next((model for model in models if model.get('name') == args.model), None)
    if model_info is None:
        raise ValueError('model is not listed by the local Ollama service')
    if not isinstance(model_info.get('digest'), str) or not model_info['digest'].strip():
        raise ValueError('local Ollama model has no verifiable digest')
    out.mkdir(parents=True, exist_ok=False)
    records = json.loads(source)
    predictions, failures, raw = {}, {}, {}
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()
    request_count = 0
    for index, record in enumerate(records):
        if stable_split(index, args.seed) != 'test':
            continue
        key = str(index)
        prompt = prompt_for(record['tokens'])
        raw[key] = []
        for attempt in range(args.retries + 1):
            body = json.dumps({'model': args.model, 'prompt': prompt, 'format': 'json',
                               'stream': False, 'think': False,
                               'options': {'temperature': 0, 'seed': args.seed, 'num_predict': 1024}}).encode()
            try:
                request_count += 1
                request = Request('http://127.0.0.1:11434/api/generate', data=body,
                                  headers={'Content-Type': 'application/json'})
                with urlopen(request, timeout=180) as response:
                    reply = json.load(response)
                raw[key].append(reply)
                value = json.loads(reply['response'])
                if not isinstance(value, dict):
                    raise ValueError('model response must be an object')
                value.update(uncertainty=1, impact=1)
                validate_maintie_prediction({'tokens': record['tokens']}, value, key)
                predictions[key] = value
                break
            except (ValueError, KeyError, TypeError, OSError) as exc:
                failures[key] = str(exc)
                if attempt < args.retries:
                    prompt = (prompt_for(record['tokens']) + '\nPrevious output was invalid: ' + str(exc) +
                              '. Return a corrected complete JSON object. Entity indices run from 0 to the number of entities minus 1.')
        if key in predictions:
            failures.pop(key, None)
    (out / 'responses.json').write_text(json.dumps(raw, ensure_ascii=False, indent=2) + '\n', encoding='utf8', newline='\n')
    (out / 'failures.json').write_text(json.dumps(failures, ensure_ascii=False, indent=2) + '\n', encoding='utf8', newline='\n')
    if not failures:
        (out / 'predictions.json').write_text(json.dumps(predictions, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf8', newline='\n')
    metadata = {'source_kind': 'local_model', 'model': args.model, 'seed': args.seed,
                'model_digest': model_info.get('digest'), 'model_details': model_info.get('details'),
                'hash_scheme': 'utf8-lf-v1',
                'source_sha256': hash_file(args.input), 'started_utc': started,
                'elapsed_seconds': time.monotonic() - t0, 'valid_count': len(predictions),
                'request_count': request_count,
                'failed_count': len(failures), 'api_fee': 0,
                'api_fee_reason': 'local Ollama endpoint; excludes machine and human costs',
                'prompt_template_sha256': hashlib.sha256(prompt_for([]).encode()).hexdigest(),
                'configuration': {'temperature': 0, 'num_predict': 1024, 'format': 'json', 'retries': args.retries}}
    metadata['prediction_sha256'] = hash_file(out / 'predictions.json') if not failures else None
    (out / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf8', newline='\n')
    print(json.dumps(metadata, indent=2))
    if failures:
        raise SystemExit('Invalid or missing responses; no complete prediction file was written')


if __name__ == '__main__':
    main()
