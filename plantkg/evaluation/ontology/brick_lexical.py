"""Freeze a lexical Brick migration baseline without reading replacement assertions."""
import argparse
import json
from difflib import SequenceMatcher
from pathlib import Path

from pyoxigraph import Literal, NamedNode
from .real_data import BRICK, DEPRECATED, RDF_TYPE, sha, triples

OWL_CLASS = 'http://www.w3.org/2002/07/owl#Class'


def generate(old_path, new_path):
    old = {q.subject.value for q in triples(old_path)
           if isinstance(q.subject, NamedNode) and q.subject.value.startswith(BRICK)}
    new = triples(new_path)
    deprecated = {q.subject.value for q in new
                  if isinstance(q.subject, NamedNode) and q.predicate.value == DEPRECATED
                  and isinstance(q.object, Literal) and q.object.value in ('true', '1')}
    candidates = {q.subject.value for q in new
                  if isinstance(q.subject, NamedNode) and q.subject.value.startswith(BRICK)
                  and q.predicate.value == RDF_TYPE and isinstance(q.object, NamedNode)
                  and q.object.value == OWL_CLASS and q.subject.value not in deprecated}
    if not candidates:
        raise ValueError('no non-deprecated Brick classes found')
    candidates = sorted(candidates)
    predictions = {}
    for term in sorted(old & deprecated):
        name = term.removeprefix(BRICK).lower().replace('_', '')
        predictions[term] = max(candidates, key=lambda candidate: (
            SequenceMatcher(None, name, candidate.removeprefix(BRICK).lower().replace('_', '')).ratio(),
            candidate))
    return predictions


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('old'); p.add_argument('new'); p.add_argument('--output', required=True)
    args = p.parse_args()
    result = generate(args.old, args.new)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf8', newline='\n') as f:
        json.dump(result, f, sort_keys=True, indent=2)
        f.write('\n')
    print(json.dumps({'method': 'lexical SequenceMatcher, excluding deprecated candidates',
                      'old_sha256': sha(args.old), 'new_sha256': sha(args.new),
                      'prediction_sha256': sha(output), 'prediction_count': len(result)}))


if __name__ == '__main__':
    main()
