"""An explicit identifier-only BTS relation baseline, not a PlantKG prediction."""
import argparse
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from pyoxigraph import NamedNode
from .evaluation import evaluate_records
from .real_data import BRICK, RELATIONS, bts_graph, triples, sha
from .__main__ import chart


def run(gold_path, seed):
    quads = triples(gold_path)
    # Subjects are observable unlabeled IDs; predicate, object, class and graph edges
    # are deliberately not passed to the predictor.
    subjects = {q.subject.value for q in quads if isinstance(q.subject, NamedNode)}
    parent_of = {}
    for subject in subjects:
        if '#' not in subject:
            continue
        base, local = subject.rsplit('#', 1)
        parts = local.split('.')
        if len(parts) >= 3:
            parent = base + '#' + parts[0]
            if parent in subjects:
                parent_of[subject] = parent
    counts = Counter(parent_of.values())
    _, gold_edges = bts_graph(gold_path)
    gold_by_subject = {}
    for s, p, o in gold_edges:
        gold_by_subject.setdefault(s, []).append((s, p.removeprefix(BRICK), o))
    rows = []
    for s in sorted(subjects):
        predicted = [(s, 'isPointOf', parent_of[s])] if s in parent_of else []
        rows.append({'id': s, 'gold': {'entities': [], 'relations': gold_by_subject.get(s, [])},
                     'pred': {'entities': [], 'relations': predicted},
                     'uncertainty': 1.0, 'impact': counts.get(s, 0) + 1})
    result = evaluate_records(rows, seed)
    result.update({'data_kind': 'derived', 'method': 'identifier-only heuristic baseline, not B30',
                   'gold_sha256': sha(gold_path), 'seed': seed,
                   'uncertainty': 'constant 1; no calibrated model confidence',
                   'impact': 'number of predicted children plus 1',
                   'scope': 'asserted exact directed triples; all RDF subjects; oracle record corrections'})
    # The candidate graph is derived only from identifiers, and supports standalone scoring.
    pred_nt = ''.join(f'{NamedNode(s)} {NamedNode(BRICK+"isPointOf")} {NamedNode(o)} .\n'
                      for s,o in sorted(parent_of.items()))
    result['prediction_sha256'] = hashlib.sha256(pred_nt.encode()).hexdigest()
    return result, pred_nt


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('gold'); p.add_argument('--seed', type=int, default=42); p.add_argument('--output', required=True)
    a = p.parse_args(); result, pred = run(a.gold, a.seed)
    out = Path(a.output); out.mkdir(parents=True, exist_ok=False)
    for name, text in (('prediction.nt', pred), ('results.json', json.dumps(result, indent=2)+'\n'),
                       ('relation-review.svg', chart(result, field='relation_f1'))):
        temp = out/(name + '.tmp')
        temp.write_text(text, encoding='utf8', newline='\n')
        os.replace(temp, out/name)


if __name__ == '__main__':
    main()
