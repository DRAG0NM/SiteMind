"""Exact-match metrics and controlled review simulation on frozen predictions."""
import hashlib
import json
import math
import random
import re
from pathlib import Path

from plantkg.core.hashing import hash_file


def stable_split(key, seed):
    value = int(hashlib.sha256(f'{seed}:{key}'.encode()).hexdigest()[:16], 16) % 100
    return 'train' if value < 70 else 'validation' if value < 80 else 'test'


def metrics(gold, predicted):
    gold, predicted = set(gold), set(predicted)
    tp = len(gold & predicted)
    precision = tp / len(predicted) if predicted else 0.0
    recall = tp / len(gold) if gold else 0.0
    return {'tp': tp, 'fp': len(predicted - gold), 'fn': len(gold - predicted),
            'precision': precision, 'recall': recall,
            'f1': 2 * precision * recall / (precision + recall) if precision + recall else 0.0}


def _freeze(value):
    return tuple(_freeze(x) for x in value) if isinstance(value, (tuple, list)) else value


def _tuples(records, field, side):
    return {(r['id'], *_freeze(x)) for r in records for x in r[side].get(field, [])}


def evaluate_records(records, seed=42):
    """Gold/pred arrays contain hashable entity and directed-relation tuples per item.

    Review simulates an oracle correcting the entire selected record, including misses.
    Uncertainty and impact must come from predictions, never from gold labels.
    """
    records = list(records)
    if len({r['id'] for r in records}) != len(records):
        raise ValueError('duplicate record id')
    gold_e, gold_r = (_tuples(records, f, 'gold') for f in ('entities', 'relations'))
    pred_e, pred_r = (_tuples(records, f, 'pred') for f in ('entities', 'relations'))
    result = {'items': len(records), 'entities': metrics(gold_e, pred_e),
              'relations': metrics(gold_r, pred_r), 'review_curves': {}}
    for field, gold, pred in (('entities', gold_e, pred_e), ('relations', gold_r, pred_r)):
        labels = {x[3] if field == 'entities' else x[2] for x in gold | pred}
        result[field + '_by_label'] = {str(label): metrics(
            {x for x in gold if (x[3] if field == 'entities' else x[2]) == label},
            {x for x in pred if (x[3] if field == 'entities' else x[2]) == label}) for label in sorted(labels)}
    shuffled = records.copy()
    random.Random(seed).shuffle(shuffled)
    orders = {'random': shuffled,
              'uncertainty': sorted(shuffled, key=lambda r: r['uncertainty'], reverse=True),
              'uncertainty_x_impact': sorted(shuffled, key=lambda r: r['uncertainty'] * r['impact'], reverse=True)}
    for name, ordered in orders.items():
        current = {r['id']: r for r in records}
        points = []
        for budget in range(len(records) + 1):
            if budget:
                r = ordered[budget - 1]
                current[r['id']] = {**r, 'pred': r['gold']}
            rows = list(current.values())
            points.append({'reviewed': budget,
                           'entity_f1': metrics(gold_e, _tuples(rows, 'entities', 'pred'))['f1'],
                           'relation_f1': metrics(gold_r, _tuples(rows, 'relations', 'pred'))['f1']})
        result['review_curves'][name] = points
    return result


FACTORY_PATTERN = re.compile(r'\b(?:air\s*condition(?:er|ing)|hvac|ventilat\w*|pump\w*|compressor\w*|electric\w*|motor\w*|lift\w*|hoist\w*)\b', re.I)

# Top-level labels from the pinned MaintIE scheme, not inferred from the test gold.
ENTITY_LABELS = ('PhysicalObject', 'Activity', 'State', 'Process', 'Property')
RELATION_LABELS = ('isA', 'contains', 'hasPart', 'hasParticipant', 'hasProperty')


def validate_maintie_prediction(item, prediction, key):
    """Validate frozen predictions using only public tokens and label definitions."""
    def fail(message):
        raise ValueError(f'prediction {key}: {message}')
    if not isinstance(prediction, dict):
        fail('expected an object')
    for field in ('entities', 'relations'):
        if not isinstance(prediction.get(field), list):
            fail(f'{field} must be an array')
    for field in ('uncertainty', 'impact'):
        value = prediction.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            fail(f'{field} must be a finite non-negative number')
    for entity in prediction['entities']:
        if not isinstance(entity, dict):
            fail('entity must be an object')
        start, end, label = entity.get('start'), entity.get('end'), entity.get('type')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(item['tokens']):
            fail('entity token span is invalid or out of bounds')
        if not isinstance(label, str) or label.split('/')[0] not in ENTITY_LABELS:
            fail(f'unknown entity label {label!r}')
    for relation in prediction['relations']:
        if not isinstance(relation, dict):
            fail('relation must be an object')
        label = relation.get('type')
        if not isinstance(label, str) or label.split('/')[0] not in RELATION_LABELS:
            fail(f'unknown relation label {label!r}')
        for field in ('head', 'tail'):
            index = relation.get(field)
            if type(index) is not int or not 0 <= index < len(prediction['entities']):
                fail(f'relation {field} must index a predicted entity')


def factory_match(text):
    return bool(FACTORY_PATTERN.search(text))


def maintie_records(gold, predictions, seed=42, split='test', factory_only=False):
    """Convert MaintIE spans (token offsets, top-level labels) and directed edges.

    Factory subset is a documented keyword proxy, not expert domain annotation.
    Predictions use MaintIE's same entities/relations shape, keyed by source index.
    """
    def convert(item):
        entities = [(e['start'], e['end'], e['type'].split('/')[0]) for e in item.get('entities', [])]
        relations = [(entities[r['head']], r['type'].split('/')[0], entities[r['tail']]) for r in item.get('relations', [])]
        return {'entities': entities, 'relations': relations}
    rows = []
    for i, item in enumerate(gold):
        if stable_split(i, seed) != split or (factory_only and not factory_match(item['text'])):
            continue
        key = str(i)
        if key not in predictions:
            raise ValueError(f'missing prediction for {key}; do not treat missing runs as empty predictions')
        pred = predictions[key]
        validate_maintie_prediction(item, pred, key)
        rows.append({'id': key, 'gold': convert(item), 'pred': convert(pred),
                     'uncertainty': pred['uncertainty'], 'impact': pred['impact']})
    return rows


def evaluate_ontology(split, seed):
    """Evaluate a JSON file of normalized records already belonging to one split."""
    source = Path(split)
    data = json.loads(source.read_text(encoding='utf-8'))
    result = evaluate_records(data['records'], seed)
    result.update(seed=seed, source=source.as_posix(), source_sha256=hash_file(source),
                  data_kind=data['data_kind'], run_id=data['run_id'])
    return result
