"""Summarize measured B30 batches from a read-only database or an exported JSON file."""
import argparse
import hashlib
import json
import math
import os
import sqlite3
from pathlib import Path
from xml.sax.saxutils import escape

SCHEMA_VERSION = 1
TOOL_VERSION = "b48-batch-report-1"


def read_batches(path, plant_id=None):
    path = Path(path).resolve()
    if path.suffix.lower() == '.json':
        return json.loads(path.read_text(encoding='utf8'))
    if not plant_id:
        raise ValueError('plant_id is required for a database')
    con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        rows = con.execute('SELECT batch_id, body FROM b30_batches WHERE plant_id=? ORDER BY rowid', (plant_id,)).fetchall()
    finally:
        con.close()  # #153：sqlite3 的 with 只提交/回滚，不关闭连接；Windows 上会锁住文件
    return [{'batch_id': key, **json.loads(body)} for key, body in rows]


def _write(path: Path, text: str) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf8", newline="\n")
    os.replace(temp, path)


def canonical_input_sha256(rows):
    """批次哈希绑定内容而非文件布局：VACUUM/拷贝后同一逻辑批次哈希不变（#179）。"""
    payload = json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def summarize(rows):
    out = []
    cumulative = 0
    cumulative_known = True
    for row in rows:
        n = row['items']
        documents = row.get('document_count')
        if documents is not None and (type(documents) is not int or documents < 0):
            raise ValueError('document_count must be a non-negative integer or null')
        cumulative_known = cumulative_known and documents is not None
        if documents is not None:
            cumulative += documents
        minutes = row.get('review_minutes')
        if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not math.isfinite(minutes) or minutes < 0):
            raise ValueError('review_minutes must be finite and non-negative or null')
        out.append({'batch_id': row['batch_id'], 'items': n,
                    'document_count': documents, 'cumulative_document_count': cumulative if cumulative_known else None,
                    'review_minutes': minutes,
                    'review_minutes_per_100_documents': 100 * minutes / documents if minutes is not None and documents else None,
                    'agent_fraction': row['agent_fraction'], 'template_fraction': 1-row['agent_fraction'],
                    'review_minutes_per_100_candidates': row['review_minutes_per_100'],
                    'tokens': row['tokens'], 'elapsed_seconds': row['elapsed_seconds'],
                    'accuracy': row.get('accuracy'), 'cost': row.get('cost')})
    return out


def svg(rows):
    # Separate scaled panels; missing values create gaps rather than false zeroes.
    fields = [('agent_fraction', 'Agent fraction'), ('template_fraction', 'Template fraction'),
              ('review_minutes_per_100_documents', 'Review min / 100 documents'),
              ('accuracy', 'Accuracy'), ('cost', 'Measured cost')]
    lines = ['<svg xmlns="http://www.w3.org/2000/svg" width="800" height="900">',
             '<rect width="100%" height="100%" fill="white"/>']
    for panel, (field, label) in enumerate(fields):
        top = 20 + panel * 170
        vals = [r.get(field) for r in rows]
        scale = max([float(x) for x in vals if x is not None] + [1])
        lines.append(f'<text x="30" y="{top+15}">{escape(label)} (max {scale:g})</text>')
        lines.append(f'<path d="M50 {top+25} V{top+135} H750" fill="none" stroke="black"/>')
        previous = None
        for i, value in enumerate(vals):
            if value is None:
                previous = None
                continue
            x = 60 + (680*i/max(1,len(rows)-1)); y = top+135-100*float(value)/scale
            if previous:
                lines.append(f'<line x1="{previous[0]:.2f}" y1="{previous[1]:.2f}" x2="{x:.2f}" y2="{y:.2f}" stroke="#2874a6"/>')
            lines.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="#2874a6"/>')
            previous = (x,y)
    return '\n'.join(lines + ['</svg>']) + '\n'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('input'); p.add_argument('--plant-id'); p.add_argument('--output', required=True)
    args = p.parse_args()
    batches = read_batches(args.input, args.plant_id)
    rows = summarize(batches)
    out = Path(args.output); out.mkdir(parents=True, exist_ok=False)
    result = {'schema_version': SCHEMA_VERSION, 'tool_version': TOOL_VERSION,
              'input_sha256': canonical_input_sha256(batches), 'plant_id': args.plant_id,
              'batches': rows, 'note': 'B30 counts candidates, not source documents; accuracy/cost require separately supplied measurements.'}
    _write(out/'batches.json', json.dumps(result, indent=2, ensure_ascii=False)+'\n')
    _write(out/'trends.svg', svg(rows))


if __name__ == '__main__':
    main()
