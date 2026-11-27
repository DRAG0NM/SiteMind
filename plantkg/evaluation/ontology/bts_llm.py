"""Zero-shot local-model BTS predictions from a pinned graph; not B30.

The predictor sees only what a reader of the published Site B graph can see:
anonymized identifiers, non-relational assertions and literal values. Types,
the four target relations and their inverse relations are removed before any
prompt is built, so gold labels never reach the model. Concept items are
closed-set labels over the classes that appear in the dataset; relation items
are four-way typing over sampled asserted pairs, where pair existence is given
and only the relation type is predicted.
"""
import argparse
import hashlib
import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from pyoxigraph import Literal, NamedNode

from .evaluation import metrics
from .real_data import BRICK, RELATIONS, SOURCES, bts_graph, sha, triples
from .__main__ import chart

RDF_TYPE = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#type'
# Target relations and their syntactic inverses, all removed from model input.
EXCLUDED = {BRICK + name for name in
            ('isPointOf', 'isPartOf', 'feeds', 'hasLocation', 'isFedBy', 'hasPart', 'isLocationOf')}
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


def _order(key, seed):
    return hashlib.sha256(f'{seed}:{key}'.encode()).hexdigest()


def _parent(subject, subjects):
    base, _, local = subject.rpartition('#')
    parts = local.split('.')
    if len(parts) >= 3:
        candidate = base + '#' + parts[0]
        if candidate in subjects:
            return candidate
    return None


def _local(value):
    for separator in ('#', '/'):
        if separator in value:
            return value.rsplit(separator, 1)[1]
    return value.rsplit(':', 1)[-1]


def features(gold_path):
    """Build observable-only views; no type, edge or inverse edge is retained."""
    quads = triples(gold_path)
    subjects = {q.subject.value for q in quads if isinstance(q.subject, NamedNode)}
    predicates, literals = {}, {}
    for q in quads:
        if not isinstance(q.subject, NamedNode):
            continue
        if q.predicate.value == RDF_TYPE or q.predicate.value in EXCLUDED:
            continue
        predicates.setdefault(q.subject.value, set()).add(_local(q.predicate.value))
        if isinstance(q.object, Literal):
            literals.setdefault(q.subject.value, set()).add(q.object.value[:40])
    return subjects, predicates, literals


def prompt_concepts(classes):
    return ('Label anonymized building-graph entities with one Brick class. '
            'You receive only identifiers, identifier depth and non-relational assertions; '
            'types and graph edges are withheld. Choose exactly one class per task from CLASSES. '
            'Answer with JSON only: {"results":[{"id":"<task id>","class":"<class>",'
            '"confidence":<0..1>}]} with one entry per task.\n'
            'CLASSES: ' + json.dumps(classes))


def prompt_relations():
    return ('Type the directed relation between two anonymized building-graph entities. '
            'The pair is known to be related; choose exactly one relation per task from '
            'RELATIONS. Answer with JSON only: {"results":[{"id":"<task id>",'
            '"relation":"<relation>","confidence":<0..1>}]} with one entry per task.\n'
            'RELATIONS: ' + json.dumps(list(RELATIONS)))


def _post(model, prompt, seed, num_predict):
    body = json.dumps({'model': model, 'prompt': prompt, 'format': 'json', 'stream': False,
                       'think': False,
                       'options': {'temperature': 0, 'seed': seed, 'num_predict': num_predict,
                                   # The default 4096-token window truncates batched answers.
                                   'num_ctx': 8192}}).encode()
    request = Request(BASE_URL + '/api/generate', data=body,
                      headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=180) as response:
        return json.load(response)


def _results(reply, key_field, allowed):
    value = json.loads(reply['response'])
    rows = value.get('results') if isinstance(value, dict) else value
    if not isinstance(rows, list):
        raise ValueError('response must contain a results array')
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('each result must be an object')
        key = row.get('id')
        if key in out:
            raise ValueError(f'duplicate result id {key!r}')
        value_ = row.get(key_field)
        if value_ not in allowed:
            raise ValueError(f'unknown {key_field} {value_!r}')
        confidence = row.get('confidence')
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) \
                or not 0 <= confidence <= 1:
            raise ValueError('confidence must be a number between 0 and 1')
        out[key] = (value_, float(confidence))
    return out


def _ask(model, seed, base_prompt, tasks, key_field, allowed, retries, counter, errors):
    """Send tasks in small groups; a group retries once with the error appended."""
    prompt = base_prompt + '\nTASKS: ' + json.dumps(tasks, ensure_ascii=False)
    for attempt in range(retries + 1):
        reply = None
        try:
            counter['requests'] += 1
            reply = _post(model, prompt, seed, 1600)
            got = _results(reply, key_field, allowed)
            if set(got) != {task['id'] for task in tasks}:
                raise ValueError('response must cover every task exactly once')
            return got, reply
        except (ValueError, KeyError, TypeError, OSError) as exc:
            errors.append(str(exc)[:200])
            if attempt == retries:
                return None, reply
            prompt += ('\nPrevious output was invalid: ' + str(exc) +
                       '. Return one JSON object with a results array covering every task id.')
    return None, None


def _batches(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def review_curves(rows, seed):
    """Oracle review simulation where each reviewed record is replaced by its gold.

    BTS keys are (subject, class) and (subject, relation, object) rather than
    MaintIE token spans, so this mirrors evaluation.evaluate_records without
    borrowing its span-shaped tuples.
    """
    def keyed(record, side, field):
        return {(record['id'], *item) for item in record[side][field]}
    flat_e = {item for row in rows for item in keyed(row, 'gold', 'entities')}
    flat_r = {item for row in rows for item in keyed(row, 'gold', 'relations')}
    shuffled = rows.copy()
    random.Random(seed).shuffle(shuffled)
    orders = {'random': shuffled,
              'uncertainty': sorted(shuffled, key=lambda row: row['uncertainty'], reverse=True),
              'uncertainty_x_impact': sorted(shuffled, key=lambda row: row['uncertainty'] * row['impact'],
                                             reverse=True)}
    curves = {}
    for name, ordered in orders.items():
        current = {row['id']: row for row in rows}
        points = []
        for budget in range(len(rows) + 1):
            if budget:
                row = ordered[budget - 1]
                current[row['id']] = {**row, 'pred': row['gold']}
            now = list(current.values())
            points.append({'reviewed': budget,
                           'entity_f1': metrics(flat_e, set().union(*(keyed(x, 'pred', 'entities') for x in now)))['f1'],
                           'relation_f1': metrics(flat_r, set().union(*(keyed(x, 'pred', 'relations') for x in now)))['f1']})
        curves[name] = points
    return curves


def run(gold_path, model, seed=42, concepts=150, pairs=200, batch=8, retries=1, output=None):
    if sha(gold_path) != SOURCES['bts/Site_B.ttl'][1]:
        raise ValueError('gold graph does not match the pinned Site_B SHA-256')
    info = _digest(model)
    gold_concepts, gold_relations = bts_graph(gold_path)
    subjects, predicates, literals = features(gold_path)
    classes = sorted({kind.removeprefix(BRICK) for _, kind in gold_concepts})
    concept_sample = sorted(subjects, key=lambda s: _order(s, seed))[:concepts]
    relation_gold = sorted(gold_relations, key=lambda row: _order('|'.join(row), seed))[:pairs]
    counter = {'requests': 0}
    progress = {'concepts': {}, 'relations': {}, 'failed_concepts': [], 'failed_relations': [],
                'requests': 0}
    if output and (Path(output) / 'progress.json').exists():
        progress = json.loads((Path(output) / 'progress.json').read_text(encoding='utf8'))
        counter['requests'] = progress['requests']

    def save():
        if not output:
            return
        path = Path(output) / 'progress.json'
        temp = path.with_name(path.name + '.tmp')
        temp.write_text(json.dumps(progress, indent=1) + '\n', encoding='utf8', newline='\n')
        os.replace(temp, path)

    started = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    concept_items = [{'id': f'c{index}',
                      'depth': len(subject.rpartition('#')[2].split('.')),
                      'predicates': sorted(predicates.get(subject, ()))[:8],
                      'values': sorted(literals.get(subject, ()))[:3]}
                     for index, subject in enumerate(concept_sample)]
    # Identifiers are opaque hex, so short aliases are used in prompts to cut
    # token cost; depth, predicates and values keep every observable feature.
    concept_alias = {f'c{index}': subject for index, subject in enumerate(concept_sample)}
    base = prompt_concepts(classes)
    for group in _batches(concept_items, batch):
        ids = {task['id'] for task in group}
        if ids <= set(progress['concepts']) | set(progress['failed_concepts']):
            continue
        got, _ = _ask(model, seed, base, group, 'class', set(classes), retries, counter,
                      progress.setdefault('errors', []))
        if got is None:
            progress['failed_concepts'].extend(task['id'] for task in group)
        else:
            progress['concepts'].update(got)
        progress['requests'] = counter['requests']
        save()
    concept_pred, failed_concepts = progress['concepts'], progress['failed_concepts']

    relation_items = [{'id': f'r{index}',
                       'subject_depth': len(s.rpartition('#')[2].split('.')),
                       'object_depth': len(o.rpartition('#')[2].split('.')),
                       'subject_predicates': sorted(predicates.get(s, ()))[:6],
                       'object_predicates': sorted(predicates.get(o, ()))[:6],
                       'subject_values': sorted(literals.get(s, ()))[:2],
                       'object_values': sorted(literals.get(o, ()))[:2]}
                      for index, (s, _, o) in enumerate(relation_gold)]
    base_r = prompt_relations()
    for group in _batches(relation_items, batch):
        ids = {task['id'] for task in group}
        if ids <= set(progress['relations']) | set(progress['failed_relations']):
            continue
        got, _ = _ask(model, seed, base_r, group, 'relation', set(RELATIONS), retries, counter,
                      progress.setdefault('errors', []))
        if got is None:
            progress['failed_relations'].extend(task['id'] for task in group)
        else:
            progress['relations'].update(got)
        progress['requests'] = counter['requests']
        save()
    failed_relations = progress['failed_relations']
    concept_pred = {concept_alias[key]: value for key, value in progress['concepts'].items()
                    if key in concept_alias}
    relation_pred = {(relation_gold[int(key.removeprefix('r'))][0], value[0],
                      relation_gold[int(key.removeprefix('r'))][2])
                     for key, value in progress['relations'].items()
                     if key.startswith('r') and key.removeprefix('r').isdigit()
                     and int(key.removeprefix('r')) < len(relation_gold)}

    # Concepts: gold and prediction restricted to the same sampled subjects.
    gold_c = {(s, c) for s, c in gold_concepts if s in set(concept_sample)}
    pred_c = {(s, BRICK + value[0]) for s, value in concept_pred.items()}
    concept_by_class = {kind: {**metrics({x for x in gold_c if x[1] == kind},
                                        {x for x in pred_c if x[1] == kind}),
                               'sample_count': len({x[0] for x in gold_c | pred_c if x[1] == kind})}
                        for kind in sorted({x[1] for x in gold_c | pred_c})}
    # Relations: typing on the same sampled pairs, so existence is given.
    gold_r = {pair for pair in relation_gold}
    pred_r = {(s, BRICK + relation, o) for s, relation, o in relation_pred}
    identifier_r = set()
    for s, _, o in relation_gold:
        if _parent(s, subjects) == o:
            identifier_r.add((s, BRICK + 'isPointOf', o))

    rows = []
    for subject in concept_sample:
        predicted_entities = ({(subject, BRICK + concept_pred[subject][0])}
                              if subject in concept_pred else set())
        gold_entities = {(subject, c) for s, c in gold_concepts if s == subject}
        subject_pairs = [pair for pair in relation_gold if pair[0] == subject]
        rows.append({
            'id': subject,
            'gold': {'entities': sorted(gold_entities),
                     'relations': [pair for pair in subject_pairs]},
            'pred': {'entities': sorted(predicted_entities),
                     'relations': [pair for pair in pred_r if pair[0] == subject]},
            'uncertainty': round(1 - concept_pred[subject][1], 6) if subject in concept_pred else 1.0,
            'impact': 1 + len(predicted_entities) + sum(pair[0] == subject for pair in pred_r),
        })
    curves = review_curves(rows, seed)

    result = {
        'data_kind': 'derived', 'method': 'zero-shot local model, not B30',
        'gold_sha256': sha(gold_path), 'seed': seed,
        'model': {'name': model, 'digest': info.get('digest'), 'details': info.get('details'),
                  'api_fee': 0, 'api_fee_reason': 'local Ollama endpoint; machine cost not measured'},
        'configuration': {'temperature': 0, 'num_predict': 1600, 'format': 'json',
                          'batch_size': batch, 'retries': retries},
        'sample': {'subjects': len(concept_sample), 'relation_pairs': len(relation_gold),
                   'strategy': 'sha256(seed:key) order over observable subjects and asserted pairs',
                   'class_count': len(classes),
                   'prompt_ids': 'short aliases c0.. and r0..; identifiers are opaque hex'},
        'concept': {'metrics': metrics(gold_c, pred_c), 'by_class': concept_by_class,
                    'scored': len(concept_sample), 'failed': len(failed_concepts)},
        'relations': {'metrics': metrics(gold_r, pred_r),
                      'by_type': {name: metrics({x for x in gold_r if x[1] == BRICK + name},
                                                {x for x in pred_r if x[1] == BRICK + name})
                                  for name in RELATIONS},
                      'scored': len(relation_gold), 'failed': len(failed_relations),
                      'protocol': 'gold pair given; only the relation type is predicted'},
        'identifier_typing_on_same_pairs': {
            'metrics': metrics(gold_r, identifier_r),
            'note': 'parent-identifier rule restricted to the same sampled pairs, for context only'},
        'review_curves': curves,
        'items': len(rows),
        'review_items': len(rows),
        'review_note': ('oracle correction of the whole subject record; uncertainty is the model '
                        'confidence, impact is prediction-side and not a measured review cost'),
        'request_count': counter['requests'],
        'started_utc': started, 'elapsed_seconds': round(time.monotonic() - t0, 2),
        'failed_count': len(failed_concepts) + len(failed_relations),
        'hash_scheme': 'utf8-lf-v1',
        'prompt_concept_sha256': hashlib.sha256(prompt_concepts([]).encode()).hexdigest(),
        'prompt_relation_sha256': hashlib.sha256(prompt_relations().encode()).hexdigest(),
    }
    concept_nt = ''.join(f'{NamedNode(s)} {NamedNode(RDF_TYPE)} {NamedNode(c)} .\n'
                         for (s, c) in sorted(pred_c)).encode()
    relation_nt = ''.join(f'{NamedNode(s)} {NamedNode(p)} {NamedNode(o)} .\n'
                          for (s, p, o) in sorted(pred_r)).encode()
    result['concept_prediction_sha256'] = hashlib.sha256(concept_nt).hexdigest()
    result['relation_prediction_sha256'] = hashlib.sha256(relation_nt).hexdigest()
    result['predictions'] = {'concept_nt': concept_nt.decode(), 'relation_nt': relation_nt.decode(),
                             'concept': {k: list(v) for k, v in sorted(concept_pred.items())},
                             'relations': [list(row) for row in sorted(relation_pred)]}
    return result, concept_nt, relation_nt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('gold', help='Pinned Site_B.ttl; only observable assertions reach the model')
    parser.add_argument('--model', required=True)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--subjects', type=int, default=150)
    parser.add_argument('--pairs', type=int, default=200)
    parser.add_argument('--batch-size', type=int, default=8)
    parser.add_argument('--retries', type=int, default=1)
    parser.add_argument('--output', required=True, help='New directory outside the repository')
    args = parser.parse_args()
    if min(args.subjects, args.pairs, args.batch_size) < 1 or args.retries < 0:
        raise ValueError('sample sizes and batch size must be positive; retries non-negative')
    repo = Path(__file__).resolve().parents[3]
    out = Path(args.output).resolve()
    if out == repo or repo in out.parents:
        raise ValueError('Raw model responses must be stored outside the repository')
    out.mkdir(parents=True, exist_ok=True)
    result, concept_nt, relation_nt = run(args.gold, args.model, args.seed, args.subjects,
                                          args.pairs, args.batch_size, args.retries, out)
    predictions = result.pop('predictions')
    for name, data in (('concept.nt', concept_nt), ('relations.nt', relation_nt)):
        (out / name).write_bytes(data)
    for name, text in (('results.json', json.dumps(result, indent=2) + '\n'),
                       ('entity-review.svg', chart(result)),
                       ('relation-review.svg', chart(result, field='relation_f1')),
                       ('responses.json', json.dumps(predictions, indent=2) + '\n')):
        temp = out / (name + '.tmp')
        temp.write_text(text, encoding='utf8', newline='\n')
        os.replace(temp, out / name)
    print(json.dumps({k: result[k] for k in ('model', 'sample', 'request_count', 'elapsed_seconds',
                                             'failed_count', 'concept', 'relations')}, indent=2))


if __name__ == '__main__':
    main()
