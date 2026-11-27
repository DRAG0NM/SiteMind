"""Freeze an embedding-similarity Brick migration baseline without reading replacements.

Class names are embedded locally (Ollama embedding endpoint) and each deprecated
term is mapped to its nearest non-deprecated class by cosine similarity. The
new ontology's `isReplacedBy` assertions are never passed to the predictor.
"""
import argparse
import json
import math
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from pyoxigraph import Literal, NamedNode

from .brick_lexical import OWL_CLASS
from .real_data import BRICK, DEPRECATED, RDF_TYPE, sha, triples

BASE_URL = 'http://127.0.0.1:11434'


def _digest(model):
    try:
        with urlopen(BASE_URL + '/api/tags', timeout=10) as response:
            models = json.load(response)['models']
    except (OSError, ValueError, KeyError, TypeError, URLError) as exc:
        raise RuntimeError('cannot verify model digest: local Ollama is not reachable') from exc
    info = next((item for item in models if item.get('name') == model), None)
    if info is None:
        raise ValueError('model is not listed by the local Ollama service')
    if not isinstance(info.get('digest'), str) or not info['digest'].strip():
        raise ValueError('local Ollama model has no verifiable digest')
    return info


def _text(name):
    return name.removeprefix(BRICK).replace('_', ' ').lower()


def embed(model, texts, batch=32):
    vectors = []
    for start in range(0, len(texts), batch):
        body = json.dumps({'model': model, 'input': texts[start:start + batch]}).encode()
        request = Request(BASE_URL + '/api/embed', data=body,
                          headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=180) as response:
                vectors.extend(json.load(response)['embeddings'])
        except (OSError, ValueError, KeyError, TypeError, URLError) as exc:
            raise RuntimeError('local embedding request failed') from exc
    if len(vectors) != len(texts):
        raise ValueError('embedding count does not match input count')
    return vectors


def _cosine(left, right):
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def generate(old_path, new_path, model):
    """Predict a replacement class from names only; the new file supplies candidates."""
    old = {q.subject.value for q in triples(old_path)
           if isinstance(q.subject, NamedNode) and q.subject.value.startswith(BRICK)}
    new = triples(new_path)
    deprecated = {q.subject.value for q in new
                  if isinstance(q.subject, NamedNode) and q.predicate.value == DEPRECATED
                  and isinstance(q.object, Literal) and q.object.value in ('true', '1')}
    candidates = sorted({q.subject.value for q in new
                         if isinstance(q.subject, NamedNode) and q.subject.value.startswith(BRICK)
                         and q.predicate.value == RDF_TYPE and isinstance(q.object, NamedNode)
                         and q.object.value == OWL_CLASS and q.subject.value not in deprecated})
    if not candidates:
        raise ValueError('no non-deprecated Brick classes found')
    terms = sorted(old & deprecated)
    info = _digest(model)
    vectors = embed(model, [_text(term) for term in terms] + [_text(name) for name in candidates])
    term_vectors, candidate_vectors = vectors[:len(terms)], vectors[len(terms):]
    predictions = {}
    for term, vector in zip(terms, term_vectors):
        scores = [(_cosine(vector, candidate_vector), candidate)
                  for candidate, candidate_vector in zip(candidates, candidate_vectors)]
        predictions[term] = max(scores)[1]
    return predictions, {'model': model, 'digest': info.get('digest'), 'details': info.get('details'),
                         'terms': len(terms), 'candidates': len(candidates)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('old')
    parser.add_argument('new')
    parser.add_argument('--model', default='plantkg-qwen3-embedding:0.6b-q8')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    predictions, metadata = generate(args.old, args.new, args.model)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf8', newline='\n') as handle:
        json.dump(predictions, handle, sort_keys=True, indent=2)
        handle.write('\n')
    print(json.dumps({'method': 'local embedding cosine similarity over class names',
                      'old_sha256': sha(args.old), 'new_sha256': sha(args.new),
                      'prediction_sha256': sha(output), 'prediction_count': len(predictions),
                      'model': metadata}, indent=2))


if __name__ == '__main__':
    main()
