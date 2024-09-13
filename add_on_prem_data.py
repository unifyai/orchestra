import json
import os
from google.cloud.storage import Client
from sqlalchemy import create_engine, insert, delete
from orchestra.db.models.orchestra_models import (
    Modality,
    Task,
    Model,
    Provider,
    Endpoint,
    Users,
)


tables = {
    "modality": {"model": Modality},
    "task": {"model": Task},
    "model": {"model": Model},
    "provider": {"model": Provider},
    "endpoint": {"model": Endpoint},
    "users": {"model": Users},
}


def get_cloud_sql_data():
    storage_client = Client()
    blob = storage_client.bucket("on-prem-data").blob("data.json")
    return json.loads(blob.download_as_bytes().decode("utf-8"))


def write_data_to_db(data, engine):
    data = {
        table: {"model": tables[table]["model"], "rows": data[table]} for table in tables
    }
    user_id = os.environ.get("USER_ID")
    data["users"] = [[f"{user_id}", 0, "", "f", 0, 0, "t"]]

    with engine.connect() as conn:
        for key, content in data.items():
            print(f"key {key}")
            if key != "users":
                stmt = delete(model)
                conn.execute(stmt)
                conn.commit()
            model = content["model"]
            rows = content["rows"]
            stmt = insert(model)
            conn.execute(stmt.values(rows))
            conn.commit()


if __name__ == "__main__":
    orchestra_db_host = os.environ.get("ORCHESTRA_DB_HOST")
    database_url = f"postgresql://orchestra:orchestra@{orchestra_db_host}/orchestra"
    engine = create_engine(database_url)
    data = get_cloud_sql_data()
    write_data_to_db(data, engine)
