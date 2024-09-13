import os
from google.cloud.sql.connector import Connector
from sqlalchemy import create_engine, text


instance_connection_name = os.environ.get("INSTANCE_CONNECTION_NAME")   #"saas-368716:europe-west3:dev"
db_user = os.environ.get("DB_USER")    #"orchestra"
db_pass = os.environ.get("DB_PASS") #"rxD7wcwWzOvLsnXhb5nDwA"
db_name = "orchestra"
connector = Connector()


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
