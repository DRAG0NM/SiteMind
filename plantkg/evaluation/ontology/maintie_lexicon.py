"""A train-only lexical MaintIE baseline: a second applicable system, not B30.

Only the train split of the pinned gold file is read. Tokens (lowercased), the
surface forms of train entities and the label/relation distributions are used
to build the lexicon; no test label reaches the predictor. The output uses the
same prediction schema as the local model so both systems share one scorer.
"""
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from plantkg.core.hashing import hash_file

from .evaluation import stable_split, validate_maintie_prediction
from .maintie_local import GOLD_SHA256

MAX_PHRASE = 4


def _spans(item):
    return [(e['start'], e['end'], e['type'].split('/')[0]) for e in item.get('entities', [])]


def build(gold, seed):
    """Collect surface-to-label counts and label-pair relation priors from train rows."""
    phrases = defaultdict(Counter)
    relations = defaultdict(Counter)
    for index, item in enumerate(gold):
        if stable_split(index, seed) != 'train':
            continue
        tokens = [token.lower() for token in item['tokens']]
        spans = _spans(item)
        for start, end, label in spans:
            phrases[tuple(tokens[start:end])][label] += 1
        for relation in item.get('relations', []):
            head, tail = spans[relation['head']], spans[relation['tail']]
            relations[(head[2], tail[2])][relation['type'].split('/')[0]] += 1
    return {key: value for key, value in phrases.items() if key}, relations


def _best(counter):
    label, count = max(counter.items(), key=lambda pair: (pair[1], pair[0]))
    return label, count / sum(counter.values())


def predict(item, phrases, relations):
    """Greedy longest surface match, then a label-pair prior for adjacent entities."""
    tokens = [token.lower() for token in item['tokens']]
    entities, certainty = [], []
    i = 0
    while i < len(tokens):
        for length in range(min(MAX_PHRASE, len(tokens) - i), 0, -1):
            counter = phrases.get(tuple(tokens[i:i + length]))
            if counter:
                label, share = _best(counter)
                entities.append({'start': i, 'end': i + length, 'type': label})
                certainty.append(share)
                i += length
                break
        else:
            i += 1
    predicted = []
    for index in range(len(entities) - 1):
        head, tail = entities[index], entities[index + 1]
        counter = relations.get((head['type'], tail['type']))
        if counter:
            label, _ = _best(counter)
            predicted.append({'head': index, 'tail': index + 1, 'type': label})
    uncertainty = 0.0 if not certainty else round(1 - sum(certainty) / len(certainty), 6)
    coverage = sum(entity['end'] - entity['start'] for entity in entities)
    return {'entities': entities, 'relations': predicted,
            'uncertainty': uncertainty, 'impact': coverage}


def generate(gold, seed=42, split='test'):
    phrases, relations = build(gold, seed)
    if not phrases:
        raise ValueError('train split produced an empty lexicon')
    predictions = {}
    for index, item in enumerate(gold):
        if stable_split(index, seed) != split:
            continue
        value = predict(item, phrases, relations)
        validate_maintie_prediction(item, value, str(index))
        predictions[str(index)] = value
    return predictions, phrases, relations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', help='Pinned MaintIE gold file; only train rows are learned from')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--split', default='test')
    parser.add_argument('--output', required=True, help='New directory outside the repository')
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[3]
    out = Path(args.output).resolve()
    if out == repo or repo in out.parents:
        raise ValueError('Experiment output must be stored outside the repository')
    if hash_file(args.input) != GOLD_SHA256:
        raise ValueError('input does not match the pinned MaintIE gold SHA-256')
    gold = json.loads(Path(args.input).read_text(encoding='utf8'))
    predictions, phrases, relations = generate(gold, args.seed, args.split)
    out.mkdir(parents=True, exist_ok=False)
    path = out / 'predictions.json'
    path.write_text(json.dumps(predictions, ensure_ascii=False, sort_keys=True, indent=2) + '\n',
                    encoding='utf8', newline='\n')
    metadata = {
        'source_kind': 'train_lexicon', 'model': None, 'seed': args.seed, 'split': args.split,
        'source_sha256': hash_file(args.input), 'prediction_sha256': hash_file(path),
        'hash_scheme': 'utf8-lf-v1', 'valid_count': len(predictions),
        'lexicon_phrases': len(phrases), 'relation_priors': len(relations),
        'uncertainty': 'one minus the mean train label share of the matched surfaces',
        'impact': 'predicted entity token coverage; a heuristic proxy, not a measured review cost',
        'method': ('supervised lexical baseline: train-only surface lexicon and label-pair '
                   'relation prior; not B30 and not a zero-shot model'),
    }
    (out / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf8', newline='\n')
    print(json.dumps(metadata, indent=2))


if __name__ == '__main__':
    main()
