"""合档签发服务：读取两份站档，核算合档，盖章并占用站档。"""
from datetime import UTC, datetime
from time import perf_counter_ns

from app.config import SPEC
from app.errors import SurveyProblem
from app.modules.ledger.identity import canonical, sha

from .engine import merge_archive
from .storage import MergeStore


def merge_seal(document, results):
    """合档内容完整性印章：覆盖合档文档与全部核算结果。

    created/elapsed_ms 属于留档元数据，不参与内容定址，重试同一对站档时印章不变。
    """
    content = {key: value for key, value in document.items()
               if key not in ('created', 'elapsed_ms')}
    return sha(canonical({'document': content, 'results': results}))


def issue(archive: MergeStore, runs, run_a, run_b):
    """签发两站合档。runs 为单站归档存储（ledger.Archive）。"""
    if run_a == run_b:
        raise SurveyProblem('MERGE_SAME_RUN', '合档需要两份不同的站档', 422)
    start = perf_counter_ns()
    manifest_a, tables_a, results_a = runs.read(run_a)
    manifest_b, tables_b, results_b = runs.read(run_b)

    # 先完成全部核算；任何业务失败都在写库之前抛出，站档不被占用、可重试。
    document, results = merge_archive([
        (manifest_a, tables_a, results_a),
        (manifest_b, tables_b, results_b),
    ])
    document['created'] = datetime.now(UTC).isoformat(timespec='seconds')
    document['elapsed_ms'] = (perf_counter_ns() - start) // 1_000_000
    seal = merge_seal(document, results)
    try:
        fresh = archive.sign(document, results, seal)
    except SurveyProblem as exc:
        if exc.code != 'STATION_ALREADY_MERGED':
            raise
        # 并发下另一请求已落定：若正是同一对站档则幂等取回，否则维持占用冲突。
        holder = archive.station_merge_id(run_a)
        if holder == document['id']:
            fresh = False
        else:
            raise
    saved, saved_results, saved_seal, _ = archive.read(document['id'])
    if saved_seal != seal or saved['result_sha256'] != document['result_sha256']:
        raise SurveyProblem('MERGE_SEAL_CONFLICT', '合档摘要与已签发内容冲突', 409)
    return dict(id=document['id'], document=saved, balance=saved_results['balance'],
                seal=saved_seal, reused=not fresh, spec=SPEC)
