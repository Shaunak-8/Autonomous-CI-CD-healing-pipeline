"""Back up and clear only the verified application tables, retaining all schema."""
import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values
from sqlalchemy import create_engine, inspect, text

TABLES = {'users', 'projects', 'runs', 'issues', 'tasks', 'iterations', 'fixes'}


def schema_snapshot(connection):
    inspector = inspect(connection)
    return {
        'tables': {
            table: {
                'columns': [(c['name'], str(c['type']), c['nullable'], str(c.get('default')))
                            for c in inspector.get_columns(table, schema='public')],
                'primary_key': inspector.get_pk_constraint(table, schema='public'),
                'foreign_keys': inspector.get_foreign_keys(table, schema='public'),
                'indexes': inspector.get_indexes(table, schema='public'),
                'unique_constraints': inspector.get_unique_constraints(table, schema='public'),
            } for table in sorted(TABLES)
        },
        'enums': inspector.get_enums(schema='public'),
        'views': inspector.get_view_names(schema='public'),
    }


def clear_data(engine, backup_directory, expected_host, expected_database):
    if engine.dialect.name != 'postgresql':
        raise ValueError('Refusing to clear a fallback or non-PostgreSQL database')
    if engine.url.host != expected_host or engine.url.database != expected_database:
        raise ValueError('Database identity does not match the explicit target')
    backup_directory = Path(backup_directory).resolve()
    backup_directory.mkdir(parents=True, exist_ok=True)
    with engine.begin() as connection:
        if connection.execute(text('SELECT current_database()')).scalar_one() != expected_database:
            raise ValueError('Connected database name differs from the expected target')
        actual = set(inspect(connection).get_table_names(schema='public'))
        if actual != TABLES:
            raise ValueError('Application table list changed; inspect the target before clearing data')
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        connection.execute(text("SET LOCAL statement_timeout = '30s'"))
        qualified = ', '.join(f'public."{name}"' for name in sorted(TABLES))
        connection.execute(text(f'LOCK TABLE {qualified} IN ACCESS EXCLUSIVE MODE'))
        schema = schema_snapshot(connection)
        rows = {name: connection.execute(text(f'SELECT row_to_json(t) FROM public."{name}" AS t')).scalars().all()
                for name in sorted(TABLES)}
        backup = {
            'created_at': datetime.now(timezone.utc).isoformat(),
            'database': expected_database, 'host': expected_host,
            'schema': schema, 'rows': rows,
        }
        path = backup_directory / f'data-before-clear-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}.json'
        with path.open('x', encoding='utf-8') as handle:
            json.dump(backup, handle, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        # Verify the backup is readable and complete before executing any deletion.
        saved = json.loads(path.read_text(encoding='utf-8'))
        if {name: len(value) for name, value in saved['rows'].items()} != {name: len(value) for name, value in rows.items()}:
            raise RuntimeError('Backup verification failed')
        # Deliberately omit CASCADE: unexpected external dependencies must abort.
        connection.execute(text(f'TRUNCATE TABLE {qualified} RESTART IDENTITY'))
        remaining = {name: connection.execute(text(f'SELECT count(*) FROM public."{name}"')).scalar_one()
                     for name in sorted(TABLES)}
        if any(remaining.values()) or schema_snapshot(connection) != schema:
            raise RuntimeError('Post-clear verification failed; transaction will roll back')
    return {'removed_rows': {name: len(value) for name, value in rows.items()},
            'remaining_rows': remaining, 'schema_preserved': True, 'backup': str(path)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clear', action='store_true', required=True)
    parser.add_argument('--expected-host', required=True)
    parser.add_argument('--expected-database', required=True)
    args = parser.parse_args()
    backend = Path(__file__).resolve().parents[1]
    url = dotenv_values(backend / '.env').get('DATABASE_URL')
    if not url:
        raise SystemExit('No configured database URL; nothing was cleared')
    engine = create_engine(url, connect_args={'connect_timeout': 10})
    try:
        result = clear_data(engine, backend / 'results' / 'database-backups', args.expected_host, args.expected_database)
        print(json.dumps(result, indent=2))
    finally:
        engine.dispose()
