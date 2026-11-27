"""离线比赛运维入口：只生成现有待确认动作，确认仍走工作台/B04。"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, Context, Role
from plantkg.core.ports import VersionStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default='demo', choices=('dev', 'demo', 'mock'))
    parser.add_argument('--set', action='append', default=[])
    parser.add_argument('--actor', required=True)
    parser.add_argument('--reason', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    review = commands.add_parser('review')
    review.add_argument('candidate_id')
    review.add_argument('--decision', choices=('accept', 'revise', 'reject'), required=True)
    review.add_argument('--revision', default='{}', help='JSON；本体填 choice；规则填 subject_id/effective_from')
    publish = commands.add_parser('publish')
    publish.add_argument('candidate_id')
    args = parser.parse_args()
    settings = load_settings(profile=args.profile, overrides=dict(item.split('=', 1) for item in args.set))
    if args.actor not in settings.actions.get('reviewers', ()):
        parser.error('actor 须是本地配置中的复核员')
    try:
        services = build_services(settings)
    except RuntimeError as exc:
        if 'SQLite writer already owns' in str(exc):
            parser.exit(2, '离线运维入口需要先暂停使用同一数据库的服务；在线复核请使用现有网关工具。\n')
        raise
    try:
        context = Context(plant_id=settings.plant_id, industry_pack=settings.industry_pack,
            actor=Actor(actor_id=args.actor, roles=(Role.reviewer,)), channel='rest',
            trace_id='document-review-cli', as_of=datetime.now(timezone.utc))
        if args.command == 'review':
            result = services.registry.tools['propose_review_decision'].handler(context,
                review_id=args.candidate_id, decision=args.decision,
                revision=json.loads(args.revision), rationale=args.reason)
        else:
            base = services.registry.resolve(VersionStore).active()
            context = context.model_copy(update={'version_manifest_id': base.version_manifest_id})
            result = services.registry.tools['propose_publish'].handler(context,
                candidate_id=args.candidate_id, rationale=args.reason)
        print(result.model_dump_json(indent=2))
        return 0 if result.ok else 1
    finally:
        services.close()


if __name__ == '__main__':
    raise SystemExit(main())
