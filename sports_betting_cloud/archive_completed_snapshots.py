"""Read-only, resumable-by-rerun export of covered, older ESPN final game snapshots.

The exported JSONL contains the entire original database row, including payload.
This script NEVER deletes or updates database rows.
"""
import argparse
import gzip
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--days', type=int, default=30)
    p.add_argument('--out', default='archive_output')
    p.add_argument('--batch-size', type=int, default=500)
    args = p.parse_args()
    if args.days < 1 or args.batch_size < 1:
        p.error('--days and --batch-size must be positive')
    target = Path(args.out)
    target.mkdir(parents=True, exist_ok=True)
    archive = target / 'covered_espn_completed_game_snapshots.jsonl.gz'
    manifest = target / 'manifest.json'
    if archive.exists() or manifest.exists():
        raise SystemExit('Refusing to overwrite an existing archive/manifest; use a fresh output directory')
    counts = {}
    total = 0
    # Keep a single consistent database snapshot for the duration of export.
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=20) as conn:
        conn.execute('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        with conn.cursor(name='completed_game_archive_cursor') as cur:
            cur.execute('''
                SELECT row_to_json(h)::text, h.sport
                FROM historical_snapshots AS h
                WHERE h.source = 'espn'
                  AND h.category = 'game'
                  AND h.event_id IS NOT NULL AND h.event_id <> ''
                  AND h.payload #>> '{status,type,completed}' = 'true'
                  AND h.collected_at < now() - (%s * interval '1 day')
                  AND EXISTS (
                    SELECT 1 FROM canonical_completed_games AS c
                    WHERE c.sport = h.sport AND c.event_id = h.event_id
                  )
                ORDER BY h.id
            ''', (args.days,))
            with gzip.open(archive, 'wt', encoding='utf-8', compresslevel=6) as output:
                while True:
                    batch = cur.fetchmany(args.batch_size)
                    if not batch:
                        break
                    for raw_json, sport in batch:
                        # Postgres row_to_json retains the full original row.
                        output.write(raw_json + '\n')
                        counts[sport] = counts.get(sport, 0) + 1
                        total += 1
                    if total % 5000 < len(batch):
                        print(f'Exported {total} rows', flush=True)
        conn.rollback()
    digest = hashlib.sha256()
    with archive.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    # Independently verify archive can be decompressed and has the expected row count.
    verified = 0
    with gzip.open(archive, 'rt', encoding='utf-8') as f:
        for line in f:
            item = json.loads(line)
            if not isinstance(item, dict) or 'payload' not in item or 'id' not in item:
                raise RuntimeError('Invalid archived row')
            verified += 1
    if verified != total:
        raise RuntimeError(f'Archive row count mismatch: {verified} vs {total}')
    info = {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'read_only': True, 'deleted_rows': 0, 'external_api_calls': False,
        'selection': 'ESPN final game snapshots older than configured days, covered by canonical_completed_games',
        'older_than_days': args.days, 'row_count': total,
        'rows_by_sport': counts, 'archive_filename': archive.name,
        'compressed_bytes': archive.stat().st_size,
        'sha256': digest.hexdigest(), 'decompression_and_json_row_count_verified': True,
        'warning': 'This is an export, not a tested database restore. Download and retain privately; GitHub artifacts expire. Do not delete source data yet.'
    }
    manifest.write_text(json.dumps(info, indent=2) + '\n')
    print(json.dumps(info, indent=2))


if __name__ == '__main__':
    main()
