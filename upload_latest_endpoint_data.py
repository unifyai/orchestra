import datetime
import json
import os
from decimal import Decimal

from google.cloud.sql.connector import Connector
from google.cloud.storage import Client
from sqlalchemy import create_engine, text

instance_connection_name = os.environ.get("INSTANCE_CONNECTION_NAME")
db_user = os.environ.get("DB_USER")
db_pass = os.environ.get("DB_PASS")
db_name = "orchestra"
connector = Connector()
tables = ["modality", "task", "model", "provider", "endpoint"]


def get_rows(conn, query):
    stmt = text(query)
    return [
        {
            col: (
                val.isoformat()
                if isinstance(val, datetime.date)
                else float(val)
                if isinstance(val, Decimal)
                else val
            )
            for col, val in row.items()
        }
        for row in conn.execute(stmt).mappings()
    ]


def get_cloud_sql_data():
    def get_conn():
        return connector.connect(
            instance_connection_name,
            "pg8000",
            user=db_user,
            password=db_pass,
            db=db_name,
        )

    # create engine
    engine = create_engine(
        "postgresql+pg8000://",
        creator=get_conn,
    )
    data = dict()

    with engine.connect() as conn:
        data = dict()
        for table in tables:
            print(table)
            rows = get_rows(conn, f"select * from {table}")
            data[table] = rows

    return data


if __name__ == "__main__":
    cloud_data = get_cloud_sql_data()
    print(cloud_data.keys())
    bucket = Client().bucket("on-prem-data")
    for key in cloud_data:
        data = cloud_data[key]
        with open(f"{key}.json", "w") as f:
            json.dump(data, f, indent=4)
        blob = bucket.blob(f"{key}.json")
        blob.upload_from_filename(f"{key}.json")
