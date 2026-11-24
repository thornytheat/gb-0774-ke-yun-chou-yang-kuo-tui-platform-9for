from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import SurveyProblem
from app.main import create_app
from app.modules.ledger.service import archive
from app.modules.ledger.storage import Archive
from app.modules.merge.engine import merge_archive
from app.modules.merge.release import release_merge
from app.modules.merge.service import issue
from app.modules.merge.storage import MergeStore
from app.modules.merge.tags import station_tag
from app.source import sources


@pytest.fixture
def stores(tmp_path, tables):
    runs = Archive(tmp_path / 'runs.db')
    merges = MergeStore(tmp_path / 'runs.db')
    return runs, merges


@pytest.fixture
def two_runs(stores):
    runs, _ = stores
    dirty = archive(runs, sources({'sample': 'dirty'}))['id']
    clean = archive(runs, sources({'sample': 'clean'}))['id']
    return dirty, clean


def _issue(stores, pair):
    runs, merges = stores
    return issue(merges, runs, *pair)


def test_merge_creates_sealed_document(stores, two_runs):
    out = _issue(stores, two_runs)
    assert out['reused'] is False and out['spec'] == 'survey-2026.1'
    document, results, seal, detail = stores[1].read(out['id'])
    assert document['kind'] == 'merged' and len(document['stations']) == 2
    assert {station['run'] for station in document['stations']} == set(two_runs)
    assert [station['run'] for station in detail] == [s['run'] for s in document['stations']]
    assert seal and results['balance']['balanced']


def test_shared_trip_population_counted_once(stores, two_runs):
    runs, _ = stores
    document, results, _, _ = stores[1].read(_issue(stores, two_runs)['id'])
    single = runs.read(two_runs[0])[2]
    # 两站出票表完全同构：共有班次占全部班次，合并人口不得翻倍
    assert document['shared_trips'] == sorted(single['frame']['trips'])
    assert results['frame']['population'] == single['frame']['population']
    assert results['balance']['population'] == 290


def test_unique_answers_from_both_stations_kept(stores, two_runs):
    runs, _ = stores
    _, results, _, _ = stores[1].read(_issue(stores, two_runs)['id'])
    dirty, clean = (runs.read(key)[2] for key in two_runs)
    expected = len(dirty['returns']['accepted']) + len(clean['returns']['accepted'])
    assert sum(results['frame']['counts'].values()) == expected
    # 两站独有答卷都在（clean 独有 00031/00032 的有效回应）
    tickets = {(row['station'], row['station_ticket']) for row in results['frame']['valid']}
    clean_tag = station_tag(two_runs[1])
    assert (clean_tag, '00031') in tickets and (clean_tag, '00032') in tickets


def test_shared_trip_new_layer_moves_both_stations(stores, two_runs):
    _, results, _, _ = stores[1].read(_issue(stores, two_runs)['id'])
    rows = [row for row in results['frame']['valid'] if row['trip'] == '0001']
    assert rows and all(row['layer'] == '枢纽' for row in rows)
    moved = {row['station_ticket'] for row in rows if row['moved']}
    # 脏站 00011 原申报城北，必须随新版改到枢纽；净站本就申报枢纽
    assert '00011' in moved


def test_cross_station_ticket_collision_kept_apart(stores, two_runs):
    _, results, _, _ = stores[1].read(_issue(stores, two_runs)['id'])
    collisions = [row for row in results['frame']['valid'] if row['station_ticket'] == '00011']
    assert len(collisions) == 2
    assert len({row['ticket'] for row in collisions}) == 2
    assert len({row['station'] for row in collisions}) == 2


def test_same_station_revisit_unchanged(stores, two_runs):
    runs, _ = stores
    _, results, _, _ = stores[1].read(_issue(stores, two_runs)['id'])
    # 脏站 00011 有两次重访，采信最高 visit 的 transfer，且只出现一次
    dirty_tag = station_tag(two_runs[0])
    winners = [row for row in results['frame']['valid']
               if row['station'] == dirty_tag and row['station_ticket'] == '00011']
    assert len(winners) == 1 and winners[0]['choice'] == 'transfer'
    # 冲突票 00024 仍按站档裁决隔离，不会被另一站“救活”
    conflict_rows = [row for row in results['returns']['rejected']
                     if row.get('station_ticket') == '00024']
    assert conflict_rows and all(row['code'] == 'VISIT_CONFLICT' for row in conflict_rows)


def test_frame_revision_conflict_rejects_merge(stores, tables):
    runs, merges = stores
    run_a = archive(runs, tables)['id']
    clash = {**tables, 'frame': tables['frame'].replace('0001,2,枢纽,101', '0001,2,枢纽,120')}
    run_b = archive(runs, clash)['id']
    with pytest.raises(SurveyProblem) as error:
        issue(merges, runs, run_a, run_b)
    assert error.value.code == 'MERGE_FRAME_CONFLICT'
    # 失败不占站档、不留合档
    assert merges.signed_station(run_a) is None and merges.signed_station(run_b) is None
    assert merges.recent() == []


def test_failure_leaves_no_partial_tables(tmp_path, tables):
    runs = Archive(tmp_path / 'p.db')
    merges = MergeStore(tmp_path / 'p.db')
    run_a = archive(runs, tables)['id']
    clash = {**tables, 'frame': tables['frame'].replace('0001,2,枢纽,101', '0001,2,枢纽,120')}
    run_b = archive(runs, clash)['id']
    with pytest.raises(SurveyProblem):
        issue(merges, runs, run_a, run_b)
    with merges.connect() as db:
        for table in ('merges', 'merge_stations', 'station_merges'):
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def test_retry_after_failure_succeeds(stores, tables):
    runs, merges = stores
    run_a = archive(runs, tables)['id']
    clash = {**tables, 'frame': tables['frame'].replace('0001,2,枢纽,101', '0001,2,枢纽,120')}
    run_bad = archive(runs, clash)['id']
    with pytest.raises(SurveyProblem):
        issue(merges, runs, run_a, run_bad)
    fixed = {**tables,
             'returns': tables['returns'] + '00091,0002,枢纽,1,YES,1/2,other\n'}
    run_good = archive(runs, fixed)['id']
    out = issue(merges, runs, run_a, run_good)
    assert out['reused'] is False and merges.signed_station(run_a) == out['id']


def test_station_can_only_join_one_signed_merge(stores, tables):
    runs, merges = stores
    run_a = archive(runs, tables)['id']
    run_b = archive(runs, {**tables,
                           'returns': tables['returns'] + '00091,0002,枢纽,1,YES,1/2,other\n'})['id']
    run_c = archive(runs, {**tables,
                           'returns': tables['returns'] + '00092,0002,枢纽,1,YES,1/2,direct\n'})['id']
    first = issue(merges, runs, run_a, run_b)
    with pytest.raises(SurveyProblem) as error:
        issue(merges, runs, run_c, run_a)
    assert error.value.code == 'STATION_ALREADY_MERGED' and error.value.status == 409
    assert merges.signed_station(run_a) == first['id']
    assert merges.signed_station(run_c) is None


def test_concurrent_requests_only_one_wins(stores, tables):
    runs, merges = stores
    run_a = archive(runs, tables)['id']
    run_b = archive(runs, {**tables,
                           'returns': tables['returns'] + '00091,0002,枢纽,1,YES,1/2,other\n'})['id']
    run_c = archive(runs, {**tables,
                           'returns': tables['returns'] + '00092,0002,枢纽,1,YES,1/2,direct\n'})['id']

    def attempt(pair):
        try:
            return 'ok', issue(merges, runs, *pair)['id']
        except SurveyProblem as exc:
            return 'err', exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, [(run_a, run_b), (run_a, run_c)]))
    assert sorted(item[0] for item in outcomes) == ['err', 'ok']
    assert [item[1] for item in outcomes if item[0] == 'err'] == ['STATION_ALREADY_MERGED']
    assert len(merges.recent()) == 1


def test_idempotent_reissue_same_identifier(stores, two_runs):
    first = _issue(stores, two_runs)
    second = _issue(stores, (two_runs[1], two_runs[0]))  # 交换甲乙顺序
    assert second['id'] == first['id'] and second['reused'] is True
    assert len(stores[1].recent()) == 1


def test_merge_independent_of_argument_order(stores, two_runs):
    doc_a, res_a = merge_archive([stores[0].read(two_runs[0]), stores[0].read(two_runs[1])])
    doc_b, res_b = merge_archive([stores[0].read(two_runs[1]), stores[0].read(two_runs[0])])
    assert doc_a['id'] == doc_b['id']
    assert doc_a['result_sha256'] == doc_b['result_sha256']
    assert res_a == res_b


def test_same_run_rejected(stores, two_runs):
    with pytest.raises(SurveyProblem) as error:
        _issue(stores, (two_runs[0], two_runs[0]))
    assert error.value.code == 'MERGE_SAME_RUN'


def test_merge_evidence_roundtrip(stores, two_runs):
    runs, merges = stores
    out = _issue(stores, two_runs)
    document, results, seal, _ = merges.read(out['id'])
    evidence = release_merge(runs, document, results, seal)
    assert evidence['verified'] and evidence['seal']
    assert set(evidence['originals']) == {s['tag'] for s in document['stations']}
    assert {len(rows) for rows in evidence['excluded'].values()}
    # 篡改合档结果即验签失败
    results['balance']['balanced'] = False
    with pytest.raises(SurveyProblem) as error:
        release_merge(runs, document, results, seal)
    assert error.value.code == 'BAD_SEAL'


def test_api_merge_flow(client):
    run_a = client.post('/api/runs', json={'sample': 'dirty'}).json['id']
    run_b = client.post('/api/runs', json={'sample': 'clean'}).json['id']
    created = client.post('/api/merges', json={'runs': [run_a, run_b]})
    assert created.status_code == 201 and created.json['reused'] is False
    merge_id = created.json['id']
    again = client.post('/api/merges', json={'runs': [run_b, run_a]})
    assert again.status_code == 200 and again.json['reused'] is True
    assert client.get('/api/merges').json['merges'][0]['id'] == merge_id
    detail = client.get(f'/api/merges/{merge_id}')
    assert detail.status_code == 200 and len(detail.json['document']['stations']) == 2
    exported = client.get(f'/api/merges/{merge_id}/export')
    assert exported.status_code == 200 and exported.json['verified']
    assert exported.headers['Content-Disposition'].startswith('attachment;')


def test_api_merge_validation(client):
    run_a = client.post('/api/runs', json={'sample': 'dirty'}).json['id']
    assert client.post('/api/merges', json={'runs': [run_a]}).status_code == 422
    assert client.post('/api/merges', json={'runs': 'x'}).status_code == 422
    missing = client.post('/api/merges', json={'runs': [run_a, 'no-such-run']})
    assert missing.status_code == 404 and missing.json['code'] == 'NO_RUN'
    assert client.get('/api/merges/missing').status_code == 404


def test_merge_pages_render(client):
    run_a = client.post('/api/runs', json={'sample': 'dirty'}).json['id']
    run_b = client.post('/api/runs', json={'sample': 'clean'}).json['id']
    merge_id = client.post('/api/merges', json={'runs': [run_a, run_b]}).json['id']
    assert client.get(f'/survey/archive?merge={merge_id}').status_code == 200
    assert client.get(f'/survey/release?merge={merge_id}').status_code == 200
    assert client.get(f'/survey/frame?merge={merge_id}').status_code == 200


def test_merge_store_reopens_across_app(tmp_path, tables):
    path = tmp_path / 'lasting.db'
    app = create_app(path)
    with app.app_context():
        from app.api import merge_storage, storage
        run_a = archive(storage(), sources({'sample': 'dirty'}))['id']
        run_b = archive(storage(), sources({'sample': 'clean'}))['id']
        out = issue(merge_storage(), storage(), run_a, run_b)
    reopened = create_app(path).test_client()
    response = reopened.get(f"/api/merges/{out['id']}")
    assert response.status_code == 200 and response.json['balance']['balanced']


def test_storage_integrity_error_rolls_back(tmp_path, tables):
    runs = Archive(tmp_path / 'roll.db')
    merges = MergeStore(tmp_path / 'roll.db')
    run_a = archive(runs, tables)['id']
    run_b = archive(runs, {**tables,
                           'returns': tables['returns'] + '00091,0002,枢纽,1,YES,1/2,other\n'})['id']
    with merges.connect() as db:
        db.execute(
            "CREATE TRIGGER reject_station BEFORE INSERT ON station_merges "
            "BEGIN SELECT RAISE(ABORT,'blocked'); END")
    with pytest.raises(SurveyProblem) as error:
        issue(merges, runs, run_a, run_b)
    # 底层约束错误统一翻译成占用冲突，事务整体回滚
    assert error.value.code == 'STATION_ALREADY_MERGED'
    with merges.connect() as db:
        for table in ('merges', 'merge_stations', 'station_merges'):
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    # 拆除触发器后重试成功，证明失败不占用站档
    with merges.connect() as db:
        db.execute('DROP TRIGGER reject_station')
    assert issue(merges, runs, run_a, run_b)['reused'] is False
