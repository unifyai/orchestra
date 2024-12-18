import json
import sys
from datetime import datetime

import requests
from sqlalchemy import create_engine, delete, insert

from orchestra.db.models.orchestra_models import (
    ApiKey,
    AuthUser,
    Endpoint,
    Modality,
    Model,
    Provider,
    Task,
    Users,
)

data_tables = [
    ("modality", Modality),
    ("task", Task),
    ("model", Model),
    ("provider", Provider),
    ("endpoint", Endpoint),
]
user_tables = [
    ("auth_user", AuthUser),
    ("api_key", ApiKey),
    ("users", Users),
]


def get_cloud_sql_data():
    data = {}
    for table in data_tables:
        table_name = table[0]
        if table_name == "users":
            continue
        print(f"downloading {table_name}")
        url = f"https://storage.googleapis.com/on-prem-data/{table_name}.json"
        data[table_name] = json.loads(requests.get(url).text)
    return data


def write_data_to_db(data, engine, user_id, email_id, api_key):
    data["auth_user"] = [
        {
            "id": user_id,
            "email": email_id,
            "name": "",
            "last_name": "",
            "job_title": "",
            "tier": "developer",
            "queries_enabled": True,
            "evaluations_enabled": True,
            "created_at": datetime.now(),
            "image": "",
        },
    ]
    data["api_key"] = [
        {
            "id": 1,
            "name": "",
            "user_id": user_id,
            "key": api_key,
            "created_at": datetime.now(),
        },
    ]
    data["users"] = [
        {
            "id": user_id,
            "credits": 10,
            "stripe_customer_id": "",
            "autorecharge": False,
            "autorecharge_threshold": -1,
            "autorecharge_qty": 0,
            "store_prompts": True,
        },
    ]
    tables = data_tables + user_tables
    data = {table[0]: {"model": table[1], "rows": data[table[0]]} for table in tables}

    with engine.connect() as conn:
        # delete the rows from the other tables
        for table in tables[::-1]:
            model = data[table[0]]["model"]
            stmt = delete(model)
            conn.execute(stmt)
            conn.commit()

        # add the data to the other tables
        for table in tables:
            table_name = table[0]
            print(f"table_name {table_name}")
            model = data[table_name]["model"]
            rows = data[table_name]["rows"]
            stmt = insert(model)
            conn.execute(stmt.values(rows))
            conn.commit()


if __name__ == "__main__":
    user_id, email_id, api_key = sys.argv[1:]
    orchestra_db_host = "localhost"
    database_url = f"postgresql://orchestra:orchestra@{orchestra_db_host}/orchestra"
    engine = create_engine(database_url)
    data = get_cloud_sql_data()
    write_data_to_db(data, engine, user_id, email_id, api_key)
