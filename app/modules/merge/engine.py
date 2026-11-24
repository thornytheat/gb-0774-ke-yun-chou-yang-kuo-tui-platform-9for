"""双站合档引擎。

合档不直接相加两站结果：共有班次的出票只能保留一份基数，而两站各自独有的
班次与答卷都要完整保留。因此这里在「已裁决数据」层面合并，再复用单站核算
管线重新分层、校准、扩推、对账。

- frame：两站出票沿革按班次/版次合并；完全相同的版本行合并出处，同版出票
  或分层不一致即拒签；共有班次以合并后的最高版次当前层为准，两站答卷统一
  跟着改层。
- returns：票号加站档命名空间，跨站撞号各自裁决、互不影响；同站重访仍按
  原有 visit 规则，裁决结论直接沿用站档已采信结果。

合档内容只取决于两份站档本身，与提交先后无关：站档按编号排序后再合并，
站档标签也只由站档编号派生。
"""
from collections import Counter
from copy import deepcopy

from app.config import SPEC
from app.errors import SurveyProblem
from app.modules.allocation.service import expand
from app.modules.balance.service import review
from app.modules.frame.join import classify
from app.modules.frame.register import versions
from app.modules.ledger.identity import canonical, sha
from app.modules.weights.service import calibration

from .tags import station_tag


def merge_frames(tagged_tables):
    """合并两站出票表，返回可复核的出票沿革与合并后出票原件。

    tagged_tables 为按站档编号排序后的 (tag, tables) 序列，保证出处行顺序稳定。
    """
    merged = {}
    for tag, tables in tagged_tables:
        for trip, history in versions(tables['frame']).items():
            bucket = merged.setdefault(trip, {})
            for item in history:
                revision = item['revision']
                prior = bucket.get(revision)
                if prior and (prior['layer'], prior['tickets']) != (item['layer'], item['tickets']):
                    raise SurveyProblem(
                        'MERGE_FRAME_CONFLICT',
                        f'共有班次 {trip} 第 {revision} 版两站出票不一致，合档停止', 422)
                if prior:
                    prior['lines'].extend(f'{tag}:{line}' for line in item['lines'])
                else:
                    bucket[revision] = dict(revision=revision, layer=item['layer'],
                                            tickets=item['tickets'],
                                            lines=[f'{tag}:{line}' for line in item['lines']])
    histories = {trip: sorted(items.values(), key=lambda row: row['revision'])
                 for trip, items in sorted(merged.items())}
    lines = ['trip,revision,layer,tickets']
    for trip, history in histories.items():
        for item in history:
            lines.append(f"{trip},{item['revision']},{item['layer']},{item['tickets']}")
    return histories, '\n'.join(lines) + '\n'


def _namespaced(record, tag):
    """给站档已裁决答卷加盖站档命名空间，其余字段保持原件值。"""
    row = deepcopy(record)
    original_ticket = record.get('ticket')
    if original_ticket is not None:
        row['ticket'] = f'{tag}:{original_ticket}'
        row['station_ticket'] = original_ticket
    row['station'] = tag
    return row


def merge_returns(tagged_results, station_runs):
    """合并两站回收裁决：跨站票号隔离，同站裁决原样沿用。"""
    accepted, rejected = [], []
    totals = Counter()
    for tag, results in tagged_results:
        accepted.extend(_namespaced(row, tag) for row in results['returns']['accepted'])
        rejected.extend(_namespaced(row, tag) for row in results['returns']['rejected'])
        totals.update(results['returns']['totals'])
    return dict(totals=dict(totals), accepted=accepted,
                rejected=sorted(rejected, key=lambda row: (row['station'], row['line'])),
                reasons=dict(Counter(row['code'] for row in rejected)),
                stations=station_runs)


def _restore_downstream(merged_returns, frame_stage):
    """用合并后的回收与出票阶段，补齐 weights/allocation/balance 下游阶段。"""
    outputs = {'returns': merged_returns, 'frame': frame_stage}
    outputs['weights'] = calibration(outputs['frame'])
    outputs['allocation'] = expand(outputs['weights'])
    outputs['balance'] = review(outputs['returns'], outputs['frame'],
                                outputs['weights'], outputs['allocation'])
    return outputs


def _align_with_histories(merged_returns, histories):
    """与 frame.service.align 同规则，但直接使用带站档出处的合并沿革。"""
    people = Counter()
    for history in histories.values():
        last = history[-1]
        people[last['layer']] += last['tickets']
    valid, invalid, no_answer = [], [], []
    for record in merged_returns['accepted']:
        row = classify(record, histories)
        code = row.get('code')
        if code == 'NONRESPONSE':
            no_answer.append(row)
        elif code:
            invalid.append(row)
        else:
            valid.append(row)
    return dict(trips=histories, population=dict(sorted(people.items())), valid=valid,
                invalid=invalid, nonresponse=no_answer,
                reasons=dict(Counter(row['code'] for row in invalid)),
                moved=sum(row['moved'] for row in valid),
                counts={'valid': len(valid), 'invalid': len(invalid),
                        'nonresponse': len(no_answer)})


def merge_archive(station_packages):
    """执行一次完整合档核算。

    station_packages 为两份 (manifest, tables, results)；函数内部按站档编号
    排序，使同一对站档无论提交顺序如何都得到同一份合档。
    返回合档文档与全阶段结果。
    """
    packages = sorted(station_packages, key=lambda package: package[0]['id'])
    tags = [station_tag(manifest['id']) for manifest, _, _ in packages]
    if tags[0] == tags[1]:
        raise SurveyProblem('MERGE_TAG_COLLISION', '两份站档标签发生碰撞，无法隔离票号', 422)
    tagged_tables = [(tag, tables) for tag, (_, tables, _) in zip(tags, packages)]
    tagged_results = [(tag, results) for tag, (_, _, results) in zip(tags, packages)]

    histories, frame_csv = merge_frames(tagged_tables)
    station_runs = {tag: manifest['id'] for tag, (manifest, _, _) in zip(tags, packages)}
    merged_returns = merge_returns(tagged_results, station_runs)

    # 两站答卷在合并后的出票沿革上统一分层，共有班次新版改层同时作用于两站。
    frame_stage = _align_with_histories(merged_returns, histories)
    outputs = _restore_downstream(merged_returns, frame_stage)

    shared = sorted(set(packages[0][2]['frame']['trips'])
                    & set(packages[1][2]['frame']['trips']))
    stations = [dict(tag=tag, run=manifest['id'], files=manifest['files'])
                for tag, (manifest, _, _) in zip(tags, packages)]
    document = {
        'kind': 'merged',
        'spec': SPEC,
        'stations': stations,
        'shared_trips': shared,
        'frame_merged_csv': frame_csv,
        'ticket_namespace': '答卷票号以站档标签前缀隔离，前缀见 stations.tag',
    }
    document['id'] = sha(canonical({
        'stations': sorted(manifest['id'] for manifest, _, _ in packages),
        'frame': sha(frame_csv),
        'spec': SPEC,
    }))
    document['result_sha256'] = sha(canonical(outputs))
    return document, outputs
