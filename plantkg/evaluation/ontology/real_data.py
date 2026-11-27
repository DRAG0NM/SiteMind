"""Read externally stored RDF datasets; score predictions without accessing gold as input."""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from urllib.request import urlopen

from pyoxigraph import Literal, NamedNode, parse
from .evaluation import metrics

BRICK = 'https://brickschema.org/schema/Brick#'
RDF_TYPE = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#type'
DEPRECATED = 'http://www.w3.org/2002/07/owl#deprecated'
RELATIONS = ('isPointOf', 'isPartOf', 'feeds', 'hasLocation')
SOURCES = {
    'bts/Site_B.ttl': ('https://raw.githubusercontent.com/cruiseresearchgroup/DIEF_BTS/ad1f0d4a6a7405b066fa9470e161cbc3b40e2833/Site_B.ttl', '770cc0ddadf6ce7df25963b7d4e9ff1c2840947152f5e27f6e4db8fde9bdf83f'),
    'brick-1.2.1/Brick.ttl': ('https://github.com/BrickSchema/Brick/releases/download/v1.2.1/Brick.ttl', '7df37f49d7933ae9cc73dc3e1fbc18e78954c41e35899bd5da8ea47c0a6d6841'),
    'brick-1.4.0/Brick.ttl': ('https://github.com/BrickSchema/Brick/releases/download/v1.4.0/Brick.ttl', 'd336095f4cc24bbdee63ba414bb12f46dd6450f718cbe8a4b3c9be23fc26f620'),
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def download(root):
    """Only create or verify pinned files; never overwrite an existing file."""
    import subprocess
    root = Path(root).resolve()
    repo = Path(__file__).resolve().parents[3]
    if root == repo or repo in root.parents:
        raise ValueError('Raw datasets must be outside the repository')
    for name, (url, digest) in SOURCES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            if name.startswith('brick-'):
                version = path.parent.name.removeprefix('brick-')
                subprocess.run(['gh', 'release', 'download', 'v' + version, '--repo', 'BrickSchema/Brick',
                                '--pattern', 'Brick.ttl', '--dir', str(path.parent)], check=True)
            else:
                data = urlopen(url, timeout=60).read()
                if hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError('Unexpected source digest: ' + name)
                with path.open('xb') as f:
                    f.write(data)
        if sha(path) != digest:
            raise ValueError('Unexpected source digest: ' + name)


def triples(path):
    return list(parse(path=str(path)))


def bts_graph(path):
    """No inference: exact asserted type and four directed relations only."""
    concepts, relations = set(), set()
    for q in triples(path):
        if not isinstance(q.subject, NamedNode) or not isinstance(q.object, NamedNode):
            continue
        s, p, o = q.subject.value, q.predicate.value, q.object.value
        if p == RDF_TYPE and o.startswith(BRICK):
            concepts.add((s, o))
        if p in {BRICK + name for name in RELATIONS}:
            relations.add((s, p, o))
    return concepts, relations


def score_bts(gold_path, prediction_path):
    gold_c, gold_r = bts_graph(gold_path)
    pred_c, pred_r = bts_graph(prediction_path)
    concept_by_type = {}
    for label in sorted({kind for _, kind in gold_c | pred_c}):
        expected = {x for x in gold_c if x[1] == label}
        predicted = {x for x in pred_c if x[1] == label}
        concept_by_type[label] = {**metrics(expected, predicted),
                                  'sample_count': len({x[0] for x in expected | predicted})}
    return {'gold_sha256': sha(gold_path), 'prediction_sha256': sha(prediction_path),
            'concept': metrics(gold_c, pred_c), 'concept_by_type': concept_by_type,
            'relations': metrics(gold_r, pred_r),
            'relation_by_type': {name: {**metrics({x for x in gold_r if x[1] == BRICK + name},
                                                  {x for x in pred_r if x[1] == BRICK + name}),
                                        'sample_count': len({x[0] for x in gold_r | pred_r if x[1] == BRICK + name})}
                                 for name in RELATIONS}}


def brick_changes(old_path, new_path):
    old = {q.subject.value for q in triples(old_path) if isinstance(q.subject, NamedNode) and q.subject.value.startswith(BRICK)}
    new = triples(new_path)
    deprecated = {q.subject.value for q in new if isinstance(q.subject, NamedNode) and q.predicate.value == DEPRECATED
                  and isinstance(q.object, Literal) and q.object.value in ('true', '1')}
    expected = {}
    for q in new:
        if isinstance(q.subject, NamedNode) and isinstance(q.object, NamedNode) and q.predicate.value == BRICK + 'isReplacedBy':
            if q.subject.value in old & deprecated:
                expected.setdefault(q.subject.value, set()).add(q.object.value)
    return old & deprecated, expected


def migration_result(old_path, new_path, predictions):
    deprecated, expected = brick_changes(old_path, new_path)
    correct, missing, wrong = [], [], []
    for source, targets in sorted(expected.items()):
        actual = predictions.get(source)
        if actual is None:
            missing.append(source)
        elif actual in targets:
            correct.append(source)
        else:
            wrong.append({'source': source, 'prediction': actual, 'expected': sorted(targets)})
    return {'old_sha256': sha(old_path), 'new_sha256': sha(new_path),
            'old_terms_deprecated_in_new': len(deprecated), 'with_replacement': len(expected),
            'correct': correct, 'missing': missing, 'wrong': wrong,
            'accuracy': len(correct) / len(expected) if expected else None}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    fetch = sub.add_parser('fetch'); fetch.add_argument('directory')
    bts = sub.add_parser('bts'); bts.add_argument('gold'); bts.add_argument('prediction')
    bts.add_argument('--unsupported-concepts', action='store_true', help='Mark concept extraction as unsupported')
    bts.add_argument('--unsupported-relations', action='store_true', help='Mark relation extraction as unsupported')
    ver = sub.add_parser('brick'); ver.add_argument('old'); ver.add_argument('new'); ver.add_argument('prediction')
    identity = sub.add_parser('brick-identity'); identity.add_argument('old'); identity.add_argument('new')
    inventory = sub.add_parser('inventory'); inventory.add_argument('gold')
    args = p.parse_args()
    if args.command == 'fetch':
        download(args.directory)
        print(json.dumps({name: {'url': url, 'sha256': digest} for name, (url, digest) in SOURCES.items()}, indent=2))
    elif args.command == 'bts':
        result = score_bts(args.gold, args.prediction)
        for unsupported, field in ((args.unsupported_concepts, 'concept'), (args.unsupported_relations, 'relation')):
            if unsupported:
                if field == 'concept':
                    result['concept'] = None
                else:
                    result['relations'] = None
                result[field + '_by_type'] = None
                result[field + '_reason'] = 'unsupported by predictor; no metric was measured'
        print(json.dumps(result, indent=2))
    elif args.command == 'inventory':
        concepts, relations = bts_graph(args.gold)
        print(json.dumps({'dataset_sha256': sha(args.gold), 'asserted_concepts': len(concepts),
                          'subjects': len({s for s, _ in concepts}),
                          'relation_counts': dict(Counter(p.removeprefix(BRICK) for _, p, _ in relations))}, indent=2))
    elif args.command == 'brick-identity':
        old = {q.subject.value for q in triples(args.old) if isinstance(q.subject, NamedNode) and q.subject.value.startswith(BRICK)}
        result = migration_result(args.old, args.new, {term: term for term in old})
        result['method'] = 'identity baseline: retain each old term, without consulting the new ontology'
        print(json.dumps(result, indent=2))
    else:
        predictions = json.loads(Path(args.prediction).read_text(encoding='utf8'))
        result = migration_result(args.old, args.new, predictions)
        result['prediction_sha256'] = sha(args.prediction)
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
