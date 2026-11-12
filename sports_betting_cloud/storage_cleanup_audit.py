"""Read-only storage cleanup audit. No external API calls, writes, or deletions."""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

OUT = Path('storage_cleanup_audit_report.json')


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'read_only': True, 'deleted_rows': 0, 'external_api_calls': False,
        'scope': "ESPN category='game' snapshots only; no boxscore, player, odds, picks, or canonical rows",
        'warning': 'Do not delete raw snapshots until every historical training/research reader is migrated or intentionally retired.',
    }
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=20, row_factory=dict_row) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        report['table_sizes'] = [dict(r) for r in conn.execute('''
            SELECT c.relname AS table_name,
                   pg_total_relation_size(c.oid) AS total_bytes,
                   pg_size_pretty(pg_total_relation_size(c.oid)) AS total_pretty
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relname IN
              ('historical_snapshots','canonical_completed_games','picks','model_predictions')
              AND c.relkind IN ('r','p') ORDER BY total_bytes DESC''')]
        # Only candidates with an identical sport/event in compact history;
        # estimates use payload column size, not recoverable disk bytes.
        report['completed_game_snapshot_coverage'] = [dict(r) for r in conn.execute('''
            SELECT h.sport,
                COUNT(*) AS completed_snapshot_rows,
                COUNT(DISTINCT h.event_id) AS unique_completed_events,
                COUNT(*) FILTER (WHERE c.event_id IS NOT NULL) AS covered_snapshot_rows,
                COUNT(*) FILTER (WHERE c.event_id IS NULL) AS uncovered_snapshot_rows,
                COALESCE(SUM(pg_column_size(h.payload)),0)::bigint AS total_payload_bytes,
                COALESCE(SUM(pg_column_size(h.payload)) FILTER (
                    WHERE c.event_id IS NOT NULL AND h.collected_at < now() - interval '30 days'),0)::bigint
                    AS older_covered_payload_bytes,
                COUNT(*) FILTER (WHERE c.event_id IS NOT NULL
                    AND h.collected_at < now() - interval '30 days') AS older_covered_rows
            FROM historical_snapshots h
            LEFT JOIN canonical_completed_games c
                ON c.sport=h.sport AND c.event_id=h.event_id
            WHERE h.source='espn' AND h.category='game'
              AND h.event_id <> '' AND h.payload #>> '{status,type,completed}'='true'
            GROUP BY h.sport ORDER BY older_covered_payload_bytes DESC''')]
        report['all_snapshot_categories'] = [dict(r) for r in conn.execute('''
            SELECT source, category, COUNT(*) AS rows,
                COALESCE(SUM(pg_column_size(payload)),0)::bigint AS payload_bytes
            FROM historical_snapshots GROUP BY source,category ORDER BY payload_bytes DESC''')]
        conn.rollback()
    OUT.write_text(json.dumps(report, indent=2, default=str) + '\n')
    logging.info('Read-only audit complete: %s', OUT)
    for row in report['completed_game_snapshot_coverage']:
        logging.info('%s: %s covered old rows, %.2f MB payload; %s uncovered completed rows',
                     row['sport'], row['older_covered_rows'],
                     row['older_covered_payload_bytes'] / 1048576,
                     row['uncovered_snapshot_rows'])
    logging.info('No rows deleted. PostgreSQL disk space is not immediately reclaimed by DELETE; VACUUM/maintenance and quota policy matter.')


if __name__ == '__main__':
    main()
