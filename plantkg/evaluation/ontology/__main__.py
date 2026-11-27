"""Run a frozen normalized experiment and render its oracle-review curve as SVG."""
import argparse
import hashlib
import json
import os
from pathlib import Path
from .evaluation import ENTITY_LABELS, RELATION_LABELS, evaluate_ontology, evaluate_records, maintie_records, metrics
from plantkg.core.hashing import hash_file


def _write(path: Path, text: str) -> None:
    """原子写：先写同目录临时文件再替换，避免中断留下半个结果。"""
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf8", newline="\n")
    os.replace(temp, path)


def chart(result, field="entity_f1"):
    colors = {'random': '#2471a3', 'uncertainty': '#d35400', 'uncertainty_x_impact': '#218c57'}
    n = max(1, result['items'])
    label = 'entity F1' if field == 'entity_f1' else 'relation F1'
    lines = ['<svg xmlns="http://www.w3.org/2000/svg" width="760" height="420" viewBox="0 0 760 420">',
             '<rect width="100%" height="100%" fill="white"/>',
             f'<text x="65" y="25">Oracle review simulation: {label}</text>',
             '<path d="M65 40 V350 H700" fill="none" stroke="black"/>',
             '<text x="10" y="45">1.0</text><text x="20" y="350">0</text>',
             f'<text x="65" y="375">0</text><text x="680" y="375">{n}</text>',
             '<text x="310" y="405">Reviewed records</text>']
    for i, (name, points) in enumerate(result['review_curves'].items()):
        coords = ' '.join(f"{65 + 635*p['reviewed']/n:.2f},{350 - 310*p[field]:.2f}" for p in points)
        lines += [f'<polyline fill="none" stroke="{colors[name]}" stroke-width="2" points="{coords}"/>',
                  f'<text x="{75+i*210}" y="395" fill="{colors[name]}">{name}</text>']
    return '\n'.join(lines + ['</svg>']) + '\n'


def _metadata_hash_matches(path: Path, recorded: str, scheme: str | None) -> bool:
    """New records use normalized text SHA; old records may use raw byte SHA."""
    if scheme not in (None, 'utf8-lf-v1'):
        raise ValueError('unsupported run metadata hash scheme')
    if recorded == hash_file(path):
        return True
    return scheme is None and recorded == hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--maintie-predictions', help='JSON predictions keyed by original record index')
    parser.add_argument('--run-metadata', help='Optional experiment metadata JSON bound to the input and prediction hashes')
    parser.add_argument('--split', choices=['train', 'validation', 'test'], default='test')
    parser.add_argument('--factory-only', action='store_true')
    parser.add_argument('--output', required=True, help='New output directory')
    args = parser.parse_args()
    if args.run_metadata and not args.maintie_predictions:
        parser.error('--run-metadata requires --maintie-predictions')
    if args.maintie_predictions:
        gold_path, pred_path = Path(args.input), Path(args.maintie_predictions)
        rows = maintie_records(json.loads(gold_path.read_text(encoding='utf8')),
                               json.loads(pred_path.read_text(encoding='utf8')), args.seed,
                               args.split, args.factory_only)
        result = evaluate_records(rows, args.seed)
        for field, labels in (('entities_by_label', ENTITY_LABELS), ('relations_by_label', RELATION_LABELS)):
            for label in labels:
                result[field].setdefault(label, metrics(set(), set()))
                kind = field.removesuffix('_by_label')
                index = 2 if kind == 'entities' else 1
                result[field][label]['sample_count'] = sum(
                    any(value[index] == label for side in ('gold', 'pred') for value in row[side][kind])
                    for row in rows)
        gold_hash, pred_hash = hash_file(gold_path), hash_file(pred_path)
        result.update(seed=args.seed, split=args.split, factory_only=args.factory_only, data_kind='derived',
                      run_id='maintie-' + pred_hash[:12],
                      source_sha256=gold_hash, predictions_sha256=pred_hash)
        if args.run_metadata:
            metadata = json.loads(Path(args.run_metadata).read_text(encoding='utf8'))
            scheme = metadata.get('hash_scheme')
            if (not _metadata_hash_matches(gold_path, metadata.get('source_sha256'), scheme)
                    or not _metadata_hash_matches(pred_path, metadata.get('prediction_sha256'), scheme)
                    or metadata.get('seed') != args.seed):
                raise ValueError('run metadata does not match source, prediction, or seed')
            result['run_metadata'] = metadata
    else:
        result = evaluate_ontology(args.input, args.seed)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    _write(out / 'results.json', json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    _write(out / 'review.svg', chart(result))
    _write(out / 'relation-review.svg', chart(result, field='relation_f1'))
    print(json.dumps({'results': str(out / 'results.json'), 'chart': str(out / 'review.svg')}))


if __name__ == '__main__':
    main()
