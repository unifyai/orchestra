import json
import sys
from google.cloud import storage


def delete_dir(bucket_name: str, dir_name: str) -> None:
    bucket = storage.Client().bucket(bucket_name)

    if not dir_name.endswith("/"):
        dir_name += "/"

    blobs = bucket.list_blobs(prefix=dir_name)

    # Delete each blob
    for blob in blobs:
        blob.delete()


if __name__ == "__main__":
    msg = sys.argv[1]
    msg = json.loads(msg)
    directory = msg["user_id"] + "/" + msg["name"]
    delete_dir(bucket_name="custom_router_data", dir_name=directory)
