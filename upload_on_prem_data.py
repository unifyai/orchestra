import json
import os
from google.cloud.sql.connector import Connector
from google.cloud.storage import Client
from sqlalchemy import create_engine, text


instance_connection_name = os.environ.get("INSTANCE_CONNECTION_NAME")
db_user = os.environ.get("DB_USER")
db_pass = os.environ.get("DB_PASS")
db_name = "orchestra"
connector = Connector()
tables = ["modality", "task", "model", "provider", "endpoint"]


def get_cloud_sql_data(tables):
    def get_conn():
        return connector.connect(
            instance_connection_name,
            "pg8000",
            user=db_user,
            password=db_pass,
            db=db_name,
        )

    engine = create_engine(
        "postgresql+pg8000://",
        creator=get_conn,
    )
    data = dict()

    with engine.connect() as conn:
        for table in tables:
            stmt = text(f"select * from {table}")
            rows = [list(row) for row in conn.execute(stmt).fetchall()]
            data[table] = rows

    return data


if __name__ == "__main__":
    cloud_data = get_cloud_sql_data()
    storage_client = Client()
    blob = storage_client.bucket("on-prem-data").blob("data.json")
    blob.upload_from_string(
        json.dumps(cloud_data, indent=4), content_type="application/json"
    )
