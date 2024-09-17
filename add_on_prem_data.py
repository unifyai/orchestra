import json
import os

import requests
from sqlalchemy import create_engine, delete, insert, select

from orchestra.db.models.orchestra_models import (
    Dataset,
    DatasetPrompt,
    Endpoint,
    Evaluator,
    Modality,
    Model,
    Provider,
    StoredPrompt,
    Task,
    Users,
)

tables = [
    ("modality", Modality),
    ("task", Task),
    ("model", Model),
    ("provider", Provider),
    ("endpoint", Endpoint),
]
hermes_tables = [
    ("users", Users),
    ("dataset", Dataset),
    ("stored_prompt", StoredPrompt),
    ("dataset_prompt", DatasetPrompt),
    # ("stored_prompt_response", StoredPromptResponse),
    ("evaluator", Evaluator),
    # ("judgement", Judgement),
    # ("evaluation", Evaluation),
]


def get_cloud_sql_data():
    data = {}
    for table in tables + hermes_tables:
        table_name = table[0]
        if table_name == "users":
            continue
        print(f"downloading {table_name}")
        url = f"https://storage.googleapis.com/on-prem-data/{table_name}.json"
        data[table_name] = json.loads(requests.get(url).text)
    return data


def write_data_to_db(data, engine):
    # add current and default users
    user_id = os.environ.get("USER_ID")
    data["users"] = [
        {
            "id": user_id,
            "credits": 0,
            "stripe_customer_id": "",
            "autorecharge": False,
            "autorecharge_threshold": 0,
            "autorecharge_qty": 0,
            "store_prompts": True,
        },
        {
            "id": "clummoqze00002hdndizy7339",
            "credits": 0,
            "stripe_customer_id": "",
            "autorecharge": False,
            "autorecharge_threshold": 0,
            "autorecharge_qty": 0,
            "store_prompts": True,
        },
    ]
    data = {
        table[0]: {"model": table[1], "rows": data[table[0]]}
        for table in tables + hermes_tables
    }

    with engine.connect() as conn:
        # check if hermes already exists in the database
        hermes = conn.execute(
            select(Dataset).where(Dataset.name == "Open Hermes"),
        ).fetchall()

        # delete the rows from the other tables
        for table in tables[::-1]:
            model = data[table[0]]["model"]
            stmt = delete(model)
            conn.execute(stmt)
            conn.commit()

        # add the data to the other tables
        for table in tables + (hermes_tables if len(hermes) == 0 else []):
            table_name = table[0]
            print(f"table_name {table_name}")
            model = data[table_name]["model"]
            rows = data[table_name]["rows"]
            stmt = insert(model)
            conn.execute(stmt.values(rows))
            conn.commit()


if __name__ == "__main__":
    orchestra_db_host = os.environ.get("ORCHESTRA_DB_HOST")
    database_url = f"postgresql://orchestra:orchestra@{orchestra_db_host}/orchestra"
    engine = create_engine(database_url)
    data = get_cloud_sql_data()
    write_data_to_db(data, engine)
