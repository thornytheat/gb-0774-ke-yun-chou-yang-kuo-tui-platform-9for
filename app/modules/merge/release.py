"""合档证据包：还原两站原件并复核合档印章。"""
from app.errors import SurveyProblem
from app.modules.ledger.identity import canonical, sha

from .service import merge_seal


def release_merge(runs, document, results, seal):
    """组装可下载的合档证据，失败原因可追溯到具体站档原件。"""
    if seal != merge_seal(document, results):
        raise SurveyProblem('BAD_SEAL', '合档结果摘要校验失败', 409)
    if sha(canonical(results)) != document['result_sha256']:
        raise SurveyProblem('BAD_SEAL', '合档核算摘要校验失败', 409)
    if not results['balance']['balanced']:
        raise SurveyProblem('UNBALANCED', '合档人口或回收行尚未对平', 409)
    originals, stations = {}, []
    for station in document['stations']:
        manifest, tables, _ = runs.read(station['run'])
        if manifest['files'] != station['files']:
            raise SurveyProblem('BAD_SEAL', f"站档 {station['tag']} 原件摘要校验失败", 409)
        originals[station['tag']] = tables
        stations.append(dict(tag=station['tag'], run=station['run'], manifest=manifest))
    payload = dict(document=document, stations=stations, originals=originals,
                   results=results,
                   excluded={'intake': results['returns']['rejected'],
                             'alignment': results['frame']['invalid'],
                             'nonresponse': results['frame']['nonresponse']})
    return dict(**payload, seal=sha(canonical(payload)), verified=True)
