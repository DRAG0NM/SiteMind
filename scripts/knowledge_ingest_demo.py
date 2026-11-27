"""创建独立、明确标注 synthetic 的资料上传验收底座；已有目录拒绝覆盖。"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, Context, Entity, Role
from plantkg.core.ports import AssetRepository, VersionStore
from plantkg.evolution.artifacts import policy_artifacts
from plantkg.world.scenario import load_scenario
from plantkg.world.scenario.__main__ import build_world


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    settings = load_settings(profile='demo', overrides={'storage.sqlite': str(args.out / 'catalog.db'),
        'storage.graph': str(args.out / 'graph'), 'storage.series': str(args.out / 'series')})
    services = build_services(settings)
    try:
        world = build_world(load_scenario('configs/scenarios/fast/b32_demo.yaml'))
        # 初始配置使用可重启校验的正式产物，避免场景进程内的临时 verifier。
        reviewer = Actor(actor_id='demo-fixture', roles=(Role.reviewer,))
        versions = services.registry.resolve(VersionStore)
        initial = policy_artifacts(services.storage.writer, settings.plant_id).prepare(world.scenario.policy_config().model_dump(mode='json'), reviewer)
        base = versions.prepare({'policy': initial}, 'synthetic 资料验收初始策略底座')
        versions.check_integrity(base.version_manifest_id)
        base = versions.activate(None, base.version_manifest_id, reviewer, 'synthetic 初始化，无知识批准')
        entity_id = world.index.eid('AC-2')
        context = Context(plant_id=settings.plant_id, industry_pack=settings.industry_pack,
            actor=reviewer, channel='rest',
            trace_id='issue-232-fixture', as_of=datetime.now(timezone.utc))
        services.registry.resolve(AssetRepository).register_entity(context, Entity(entity_id=entity_id,
            plant_id=settings.plant_id, entity_type='air_compressor', label='AC-2'))
        (args.out / 'manual-v1.txt').write_text('AC-2 空压机，主管压力5分钟内下降超过0.2 bar。\n数据性质 synthetic，仅用于本地功能验收。', encoding='utf-8')
        (args.out / 'manual-v2.txt').write_text('AC-2 空压机，主管压力5分钟内下降超过0.3 bar。\n数据性质 synthetic，仅用于本地功能验收。', encoding='utf-8')
        (args.out / 'unknown.json').write_text('{"note":"未知设备，两台同名机器；保留待人工复核，不自动关联"}', encoding='utf-8')
        evidence = {'nature': 'synthetic', 'entity_id': entity_id, 'base_manifest_id': base.version_manifest_id,
                    'sqlite': str(args.out / 'catalog.db')}
        (args.out / 'fixture.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(evidence, ensure_ascii=False))
    finally:
        services.close()


if __name__ == '__main__':
    main()
