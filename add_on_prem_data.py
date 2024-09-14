import json
import os
from google.cloud.storage import Client
from sqlalchemy import create_engine, insert, delete, text
from orchestra.db.models.orchestra_models import (
    Evaluation,
    Evaluator,
    Judgement,
    Modality,
    StoredPromptResponse,
    Task,
    Model,
    Provider,
    Endpoint,
    Users,
    Dataset,
    DatasetPrompt,
    StoredPrompt
)


tables = [
    ("modality", Modality),
    ("task", Task),
    ("model", Model),
    ("provider", Provider),
    ("endpoint", Endpoint),
    ("users", Users),
]

hermes_tables = [
    ("dataset", Dataset),
    ("dataset_prompt", DatasetPrompt),
    ("stored_prompt", StoredPrompt),
    ("stored_prompt_response", StoredPromptResponse),
    ("judgement", Judgement),
    ("evaluator", Evaluator),
    ("evaluation", Evaluation)
]


def get_cloud_sql_data():
    storage_client = Client()
    blob = storage_client.bucket("on-prem-data").blob("data.json")
    return json.loads(blob.download_as_bytes().decode("utf-8"))


def write_data_to_db(data, engine):
    data = {
        table[0]: {"model": table[1], "rows": data[table]}
        for table in tables + hermes_tables
    }
    user_id = os.environ.get("USER_ID")
    data["users"] = [[f"{user_id}", 0, "", False, 0, 0, True]]

    with engine.connect() as conn:
        # check if hermes already exists in the database
        hermes = conn.execute(text("select * from dataset where name='hermes'")).fetchall()

        # delete the rows from the other tables
        for table in tables[::-1]:
            model = data[table]["model"]
            stmt = delete(model)
            conn.execute(stmt)
            conn.commit()
        
        # add the data to the other tables
        for table in (
            tables + ([] if len(hermes) == 0 else hermes_tables)
        ):
            table_name = table[0]
            model = table[1]
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
