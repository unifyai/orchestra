import json
import os

from google.cloud.sql.connector import Connector
from google.cloud.storage import Client
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import insert

from orchestra.db.models.orchestra_models import (
    Endpoint,
    Modality,
    Model,
    Provider,
    Task,
)

TABLE_ORDER = ["modality", "task", "provider", "model", "endpoint"]
TABLE_CLASSES = {
    "modality": Modality.__table__,
    "task": Task.__table__,
    "provider": Provider.__table__,
    "model": Model.__table__,
    "endpoint": Endpoint.__table__,
}

# Conflict targets (PK columns) for each table
CONFLICT_TARGETS = {
    "modality": ["name"],
    "task": ["name"],
    "provider": ["id"],
    "model": ["id"],
    "endpoint": ["id"],
}


def get_gcs_data():
    """
    Download JSON data for each table from the configured GCS bucket.
    Returns a dict mapping table_name -> list of row dicts.
    """
    bucket = Client(project="saas-368716").bucket("on-prem-data")
    data = {}
    for table in TABLE_ORDER:
        blob = bucket.blob(f"{table}.json")
        text = blob.download_as_text()
        data[table] = json.loads(text)
    return data


def get_staging_db_engine():
    """
    Create and return a SQLAlchemy engine connected to the staging
    Cloud SQL instance using the Cloud SQL Python Connector.
    """
    instance_conn = os.environ["STAGING_INSTANCE_CONNECTION_NAME"]
    db_user = os.environ["STAGING_DB_USER"]
    db_pass = os.environ["STAGING_DB_PASS"]
    db_name = os.environ["DB_NAME"]

    connector = Connector()

    def get_conn():
        return connector.connect(
            instance_conn,
            "pg8000",
            user=db_user,
            password=db_pass,
            db=db_name,
        )

    engine = create_engine("postgresql+pg8000://", creator=get_conn)
    return engine


def upsert_table(engine, table_name, rows, conflict_target):
    """
    Upsert rows into the given table, updating on conflict
    using the provided conflict_target columns.
    Returns the number of inserted rows.
    """
    table = TABLE_CLASSES[table_name]
    stmt = insert(table).values(rows)
    # figure out which columns actually need updating
    non_pk_cols = [c.name for c in table.columns if c.name not in conflict_target]
    if non_pk_cols:
        stmt = stmt.on_conflict_do_update(
            index_elements=conflict_target,
            set_={col: getattr(stmt.excluded, col) for col in non_pk_cols},
        )
    else:
        # if no non-PK columns, fall back to a no-op on conflict
        stmt = stmt.on_conflict_do_nothing(index_elements=conflict_target)
    with engine.begin() as conn:
        result = conn.execute(stmt)
    return result.rowcount if result.rowcount is not None else 0


def main():
    data = get_gcs_data()
    engine = get_staging_db_engine()
    for table in TABLE_ORDER:
        rows = data.get(table, [])
        if not rows:
            print(f"[WARN] No data for table '{table}', skipping.")
            continue
        conflict_cols = CONFLICT_TARGETS[table]
        inserted = upsert_table(engine, table, rows, conflict_cols)
        print(f"[INFO] Table '{table}': attempted={len(rows)}, inserted={inserted}")
        exit()


if __name__ == "__main__":
    main()
