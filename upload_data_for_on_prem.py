import datetime
from decimal import Decimal
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


def get_rows(conn, query):
    stmt = text(query)
    return [
        {
            col: (
                val.isoformat()
                if isinstance(val, datetime.date)
                else float(val) if isinstance(val, Decimal)
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
        hermes = get_rows(conn, "select * from dataset where name='hermes'")[0]
        hermes_id = hermes["id"]
        print("dataset")
        dataset_prompts = get_rows(
            conn, f"select * from dataset_prompt where dataset_id={hermes_id}"
        )
        print("dataset_prompt")
        prompt_ids = [dataset_prompt["prompt_id"] for dataset_prompt in dataset_prompts]
        stored_prompts = get_rows(
            conn, f"select * from stored_prompt where id in {tuple(prompt_ids)}"
        )
        print("stored_prompt")
        stored_prompt_responses = get_rows(
            conn, f"select * from stored_prompt_response where prompt_id in {tuple(prompt_ids)}"
        )
        print("stored_prompt_response")
        response_ids = [stored_prompt_response["id"] for stored_prompt_response in stored_prompt_responses]
        default_evaluator = get_rows(
            conn, "select * from evaluator where name='default_evaluator'"
        )[0]
        print("evaluator")
        evaluator_id = default_evaluator["id"]
        judgements = get_rows(
            conn, f"select * from judgement where response_id in {tuple(response_ids)} and evaluator_id={evaluator_id}"
        )
        print("judgement")
        evaluations = get_rows(
            conn, f"select * from evaluation where prompt_id in {tuple(prompt_ids)} and evaluator_id={evaluator_id}"
        )
        print("evaluation")
        data = {
            "dataset": [hermes],
            "dataset_prompt": dataset_prompts,
            "stored_prompt": stored_prompts,
            "stored_prompt_response": stored_prompt_responses,
            "judgement": judgements,
            "evaluator": [default_evaluator],
            "evaluation": evaluations
        }

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
