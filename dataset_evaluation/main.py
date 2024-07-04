import concurrent
import json
import logging
import signal
import sys
import subprocess

import sdnotify
from google.cloud import pubsub_v1

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# Global reference to the state of the script
shutdown_flag = False

# Create an instance of sdnotify
n = sdnotify.SystemdNotifier()

# Pub/Sub subscription
subscription_name = "projects/saas-368716/subscriptions/dataset_evaluation-sub"


# Function to handle graceful shutdown
def signal_handler(signal, frame):
    global shutdown_flag
    logging.info("Received termination signal, shutting down gracefully...")
    shutdown_flag = True


# PubSub Callback to deal with the message
# NOTE: This callback needs to be idempotent as ACKs are best effort.
def pub_sub_callback(message):
    global shutdown_flag
    if not shutdown_flag:
        try:
            data = json.loads(message.data)
            logging.info(f"entry: {data}")
            user_id = data["user_id"]
            api_key = data["api_key"]
            dataset_name = data["dataset"]
            endpoint = data["endpoint"]
            orchestra_url = data["orchestra_url"]
            subprocess.Popen(
                f"venv/bin/python3 dataset_evaluation.py --user_id={user_id} --api_key={api_key} --dataset_name={dataset_name} --endpoint={endpoint} --orchestra_url={orchestra_url}",
                shell=True,
            )
        except json.decoder.JSONDecodeError:
            logging.error(f"Error parsing message: {message.data}")
        except:
            logging.error(f"Unrecognised error in message: {message.data}")
        finally:
            # acknowledge that data has been processed
            # NOTE: If the message is not acknowledged in time, pubsub will send it
            # again. To avoid this the AckDeadline can be modified.
            # The processing of the data should be as quick as possible tho.
            # (i.e. spawning a subprocess).
            # NOTE: If there is an error with the message format, it should be
            # logged and acknowledged, otherwise the message will keep coming.
            message.ack()
    else:
        message.nack()


# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

if __name__ == "__main__":
    logging.info("Starting service!")
    # Notify systemd that the service is ready
    n.notify("READY=1")

    # This method requires either gcloud to be authenticated (this is the case
    # when using a service account in a Compute Engine instance) or to have an
    # env var GOOGLE_APPLICATION_CREDENTIALS=<path_to_credentials.json> defined
    subscriber = pubsub_v1.SubscriberClient()
    future = subscriber.subscribe(subscription_name, pub_sub_callback)
    logging.info("Subscribed to topic.")

    while not shutdown_flag:
        try:
            future.result(timeout=10)
        except concurrent.futures.TimeoutError:
            logging.info("No message received")
        finally:
            n.notify("WATCHDOG=1")

    # NOTE: Any message being processed will finish before termination.
    logging.info("All tasks finished correctly! Stopping the service.")
    logging.info("Cancelling subscription...")
    future.cancel()
    subscriber.close()
    n.notify("STOPPING=1")
    sys.exit(0)
