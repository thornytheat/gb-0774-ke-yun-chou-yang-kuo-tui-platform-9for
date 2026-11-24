import json
import sqlite3
from contextlib import contextmanager, closing
from pathlib import Path

from app.errors import SurveyProblem
from app.modules.ledger.identity import canonical


class MergeStore:
    """合档留档。station_merges 的唯一约束保证一份站档只计入一份已签合档。"""

    def __init__(self, filename):
        self.filename = filename
        Path(filename).parent.mkdir(exist_ok=True, parents=True)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS merges(
                id TEXT PRIMARY KEY, created TEXT NOT NULL, document TEXT NOT NULL,
                results TEXT NOT NULL, seal TEXT NOT NULL
            ); CREATE TABLE IF NOT EXISTS merge_stations(
                merge_id TEXT NOT NULL REFERENCES merges(id),
                run_id TEXT NOT NULL, tag TEXT NOT NULL, position INTEGER NOT NULL,
                PRIMARY KEY(merge_id, run_id)
            ); CREATE TABLE IF NOT EXISTS station_merges(
                run_id TEXT PRIMARY KEY, merge_id TEXT NOT NULL
                    REFERENCES merges(id)
            );''')

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.filename, timeout=10)) as db, db:
            db.execute('PRAGMA foreign_keys=ON')
            yield db

    def signed_station(self, run_id):
        """返回站档已计入的合档编号，未签出则为 None。"""
        with self.connect() as db:
            row = db.execute('SELECT merge_id FROM station_merges WHERE run_id=?',
                             (run_id,)).fetchone()
        return row[0] if row else None

    def sign(self, document, results, seal):
        """一次事务内签发合档并占用两份站档；中途失败整体回滚，不占站档。

        幂等：同一对站档的合档编号确定，重复签发直接返回既有合档。并发下抢
        不到站档占用时抛出 STATION_ALREADY_MERGED，由上层决定重试或报错。
        """
        merge_id = document['id']
        try:
            with self.connect() as db:
                existing = db.execute('SELECT id FROM merges WHERE id=?', (merge_id,)).fetchone()
                if existing:
                    return False
                for station in document['stations']:
                    holder = db.execute('SELECT merge_id FROM station_merges WHERE run_id=?',
                                        (station['run'],)).fetchone()
                    if holder:
                        raise SurveyProblem(
                            'STATION_ALREADY_MERGED',
                            f"站档 {station['run'][:12]} 已计入合档 {holder[0][:12]}", 409)
                db.execute('INSERT INTO merges VALUES (?,?,?,?,?)',
                           (merge_id, document['created'], canonical(document),
                            canonical(results), seal))
                db.executemany(
                    'INSERT INTO merge_stations VALUES (?,?,?,?)',
                    [(merge_id, station['run'], station['tag'], position)
                     for position, station in enumerate(document['stations'])])
                db.executemany('INSERT INTO station_merges VALUES (?,?)',
                               [(station['run'], merge_id) for station in document['stations']])
        except sqlite3.IntegrityError as exc:
            # 并发事务抢先占用了站档：当前事务已回滚，站档归属先到者。
            raise SurveyProblem('STATION_ALREADY_MERGED',
                                '站档已被并发合档占用，请改用已签合档', 409) from exc
        return True

    def station_merge_id(self, run_id):
        """查站档当前归属的合档编号（含并发落定后的结果）。"""
        return self.signed_station(run_id)

    def read(self, merge_id):
        with self.connect() as db:
            row = db.execute('SELECT document,results,seal FROM merges WHERE id=?',
                             (merge_id,)).fetchone()
            stations = db.execute(
                'SELECT run_id,tag,position FROM merge_stations WHERE merge_id=? ORDER BY position',
                (merge_id,)).fetchall()
        if not row:
            raise SurveyProblem('NO_MERGE', '找不到合档签发', 404)
        document, results, seal = json.loads(row[0]), json.loads(row[1]), row[2]
        detail = [dict(run=run_id, tag=tag, position=position)
                  for run_id, tag, position in stations]
        return document, results, seal, detail

    def recent(self):
        with self.connect() as db:
            rows = db.execute('SELECT document FROM merges ORDER BY created DESC,id LIMIT 40').fetchall()
        return [json.loads(row[0]) for row in rows]
