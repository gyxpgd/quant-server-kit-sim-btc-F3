"""Atomic local paper ledger. No credentials, external writes or order access."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


@contextmanager
def process_lock(directory):
    import fcntl
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / 'process.lock').open('a+b')
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('这个数据目录已有模拟进程运行；请勿重复启动。') from None
        yield
    finally:
        handle.close()


class Store:
    def __init__(self, directory, readonly=False):
        self.directory = Path(directory).resolve()
        self.path = self.directory / 'paper.sqlite3'
        if readonly:
            self.db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=15)
        else:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(self.path), timeout=15)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY,v TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS inputs(kind TEXT,ts INTEGER,payload TEXT NOT NULL,
                observed INTEGER NOT NULL,PRIMARY KEY(kind,ts));
              CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,ts INTEGER NOT NULL,
                kind TEXT NOT NULL,classification TEXT NOT NULL,observed INTEGER NOT NULL,payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY,observed INTEGER NOT NULL,
                start INTEGER NOT NULL,end INTEGER NOT NULL,classification TEXT NOT NULL,minutes INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS schedules(ts INTEGER PRIMARY KEY,observed INTEGER NOT NULL);
            ''')

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute('SELECT v FROM kv WHERE k=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (key, encode(value)))

    def initialize(self, settings, identity, spec, raw_spec, start):
        with self.db:
            if self.get('settings') is not None:
                if self.get('settings') != settings or self.get('identity') != identity:
                    raise ValueError('配置或程序与账本不一致。保留原数据，使用新的 --data-dir 开始独立测试。')
                if self.get('spec') != spec:
                    raise ValueError('交易规格发生变化；原账本已保留，停止推进模拟。')
                return
            self.put('settings', settings)
            self.put('identity', identity)
            self.put('spec', spec)
            self.put('raw_spec', raw_spec)
            self.put('start', start)
            self.put('cursor', start)
            self.put('health', {'state': 'WAITING', 'message': '等待首根完整行情', 'observed': start})

    def verify_identity(self, settings, identity):
        if self.get('settings') is not None and (self.get('settings') != settings or self.get('identity') != identity):
            raise ValueError('配置或程序与账本不一致；请使用新的 --data-dir。')

    def ingest(self, kind, rows, observed):
        """Must be called inside a transaction; revisions are never hidden."""
        for ts, value in rows:
            payload = encode(value)
            old = self.db.execute('SELECT payload FROM inputs WHERE kind=? AND ts=?', (kind, ts)).fetchone()
            if old and old[0] != payload:
                raise ValueError('已保存公开数据发生修订：%s %s；为避免改写历史，停止推进。' % (kind, ts))
            self.db.execute('INSERT OR IGNORE INTO inputs VALUES(?,?,?,?)', (kind, ts, payload, observed))

    def rows(self, kind, start, end):
        return [json.loads(r[0]) for r in self.db.execute(
            'SELECT payload FROM inputs WHERE kind=? AND ts>=? AND ts<? ORDER BY ts', (kind, start, end))]

    def record_schedules(self, times, observed, start):
        with self.db:
            for ts in times:
                if ts >= start:
                    self.db.execute('INSERT OR IGNORE INTO schedules VALUES(?,?)', (ts, observed))

    def due_schedules(self, start, end):
        return [r[0] for r in self.db.execute('SELECT ts FROM schedules WHERE ts>=? AND ts<? ORDER BY ts', (start, end))]

    def commit_batch(self, engine, observed, start, end, classification, health, processed_funding):
        # Snapshot follows drain: the outbox and cursor are committed with events.
        with self.db:
            if self.get('cursor') != start:
                raise ValueError('账本游标不匹配，拒绝重复推进。')
            for event in engine.drain_events():
                self.db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',
                    (event['seq'], event['ts'], event['kind'], classification, observed, encode(event)))
            self.put('checkpoint', engine.snapshot())
            self.put('account', engine.status())
            self.put('cursor', end)
            self.put('health', health)
            self.put('processed_funding', processed_funding)
            self.db.execute('INSERT INTO batches(observed,start,end,classification,minutes) VALUES(?,?,?,?,?)',
                            (observed, start, end, classification, (end-start)//60000))

    def health(self, value):
        with self.db:
            self.put('health', value)

    def statistics(self):
        return {
            'events': self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],
            'minutes_by_classification': dict(self.db.execute('SELECT classification,SUM(minutes) FROM batches GROUP BY classification')),
            'events_by_classification': dict(self.db.execute('SELECT classification,COUNT(*) FROM events GROUP BY classification')),
        }

    def backup(self, target):
        destination = sqlite3.connect(str(target))
        try:
            self.db.backup(destination)
            # Export a self-contained database without depending on WAL/SHM
            # files; also permits opening the exported copy read-only.
            destination.execute('PRAGMA journal_mode=DELETE')
        finally:
            destination.close()
