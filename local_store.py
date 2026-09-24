"""Local persistence adapter, compatible with the existing record interface.

SQLite is a portable demo target; it does not establish company production approval.
No network access is performed by this module.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid


def connection():
    folder = Path(os.environ['ZL_DATA_DIR'])
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder / 'lease.sqlite3', timeout=30)
    db.execute('CREATE TABLE IF NOT EXISTS records (bucket TEXT, id TEXT PRIMARY KEY, body TEXT NOT NULL)')
    return db


def scalar(v):
    return next(iter(v.values()), '') if isinstance(v, dict) else v


def matches(row, filt):
    if not filt:
        return True
    if 'and' in filt:
        return all(matches(row, f) for f in filt['and'])
    if 'or' in filt:
        return any(matches(row, f) for f in filt['or'])
    rule = filt.get('property', {})
    if not isinstance(rule, dict) or 'property' not in rule:
        raise ValueError('Unsupported filter')
    value = scalar(row.get(rule['property']))
    for kind, ops in rule.items():
        if kind == 'property':
            continue
        for op, expected in ops.items():
            if op == 'equals' and value != expected:
                return False
            elif op == 'contains' and str(expected) not in str(value or ''):
                return False
            elif op not in ('equals', 'contains'):
                raise ValueError('Unsupported filter operator: ' + op)
    return True


def query(db, filt=None, fields=None, sorts=None, page_size=200):
    with connection() as conn:
        rows = [dict(json.loads(body), record_id=rid) for rid, body in
                conn.execute('SELECT id, body FROM records WHERE bucket=? ORDER BY rowid', (db,))]
    rows = [r for r in rows if matches(r, filt)]
    for sort in reversed(sorts or []):
        key = sort.get('property') or sort.get('field')
        rows.sort(key=lambda r: str(scalar(r.get(key)) or ''), reverse=sort.get('direction') == 'descending')
    return rows


query_all = query


def get_record(db, record_id):
    with connection() as conn:
        item = conn.execute('SELECT body FROM records WHERE bucket=? AND id=?', (db, record_id)).fetchone()
    return dict(json.loads(item[0]), record_id=record_id) if item else None


def add(db, records):
    out = []
    with connection() as conn:
        for rec in records:
            rid = uuid.uuid4().hex
            conn.execute('INSERT INTO records VALUES (?,?,?)', (db, rid, json.dumps(rec, ensure_ascii=False)))
            out.append({'success': True, 'record_id': rid})
    return out


def update_checked(db, records):
    out = []
    with connection() as conn:
        for rec in records:
            rid = rec['record_id']
            old = conn.execute('SELECT body FROM records WHERE bucket=? AND id=?', (db, rid)).fetchone()
            if old is None:
                raise ValueError('Record not found')
            body = json.loads(old[0])
            body.update(rec['properties'])
            conn.execute('UPDATE records SET body=? WHERE id=?', (json.dumps(body, ensure_ascii=False), rid))
            out.append({'success': True, 'record_id': rid})
    return out


update = update_checked


def delete(db, record_ids):
    with connection() as conn:
        for rid in record_ids:
            conn.execute('DELETE FROM records WHERE bucket=? AND id=?', (db, rid))
    return [{'success': True, 'record_id': r} for r in record_ids]


def hash_pwd(salt, pwd):
    return hashlib.sha256((salt + pwd).encode()).hexdigest()


def is_sandbox():
    return False


def set_token(token):
    pass
