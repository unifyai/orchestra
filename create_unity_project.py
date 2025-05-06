import os

from dotenv import load_dotenv
from google.cloud.sql.connector import Connector
from sqlalchemy import create_engine, text

from orchestra.db.seeding.default_tasks_seeder import DefaultTasksSeeder
from orchestra.web.api.dependencies import _ro_session

load_dotenv()
instance_connection_name = os.environ.get("INSTANCE_CONNECTION_NAME")
db_user = os.environ.get("DB_USER")
db_pass = os.environ.get("DB_PASS")
db_name = "orchestra"
connector = Connector()
tables = ["auth_user", "project", "artifact", "dataset_artifact", "log_event", "log"]


def create_unity_project():

    # Use Cloud SQL connector for GCP deployment
    from google.cloud.sql.connector import Connector

    # Validate required connection information
    if not instance_connection_name:
        raise ValueError("Missing Cloud SQL instance connection name")

    connector = Connector()

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
    import orchestra.web.lifetime as lt

    lt._engine = engine
    with _ro_session(autoflush=True, expire_on_commit=False) as session:
        try:
            result = session.execute(text("SELECT id FROM auth_user"))
            user_ids = [row[0] for row in result]
            for user_id in user_ids:
                DefaultTasksSeeder.seed(session, user_id)
                print(f"Seeded user {user_id}")
            session.commit()
        except Exception as e:
            print(e)
            session.rollback()


if __name__ == "__main__":
    create_unity_project()
