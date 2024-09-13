import os
from sqlalchemy import create_engine, insert
from get_cloud_data import get_cloud_sql_data
from orchestra.db.models.orchestra_models import (
    Modality,
    Task,
    Model,
    Provider,
    Endpoint,
    Users,
)


orchestra_db_host = os.environ.get("ORCHESTRA_DB_HOST")
database_url = f"postgresql://orchestra:orchestra@{orchestra_db_host}/orchestra"
local_engine = create_engine(database_url)

tables = {
    "modality": {"model": Modality},
    "task": {"model": Task},
    "model": {"model": Model},
    "provider": {"model": Provider},
    "endpoint": {"model": Endpoint},
    "users": {"model": Users},
}
data = get_cloud_sql_data(list(tables.keys()))
data = {
    table: {"model": tables[table]["model"], "rows": data[table]} for table in tables
}
user_id = os.environ.get("USER_ID")
data["users"] = [[f"{user_id}", 0, "", "f", 0, 0, "t"]]

with local_engine.connect() as conn:
    for key, content in data.items():
        print(f"key {key}")
        model = content["model"]
        rows = content["rows"]
        stmt = insert(model)
        conn.execute(stmt.values(rows))
        conn.commit()
