import concurrent
import json
import logging
import os
import signal
import subprocess
import sys
from queue import Queue

import redis
import sdnotify
from google.cloud import pubsub_v1

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# Global reference to the state of the script
shutdown_flag = False

# Queue for maintaining requested jobs
q = Queue()

# Current status of execution
running = False

# Create an instance of sdnotify
n = sdnotify.SystemdNotifier()

# Pub/Sub subscription
using_pubsub = not os.environ.get("ON_PREM") or os.environ.get("MESSAGING_TOPIC")
subscription_name = os.environ.get(
    "MESSAGING_TOPIC",
    "projects/saas-368716/subscriptions/dataset_evaluation-sub",
)
if os.getenv("STAGING"):
    subscription_name = (
        "projects/saas-368716/subscriptions/staging-dataset_evaluation-sub"
    )


# Function to handle graceful shutdown
def signal_handler(signal, frame):
    global shutdown_flag
    logging.info("Received termination signal, shutting down gracefully...")
    shutdown_flag = True


# PubSub Callback to deal with the message
# NOTE: This callback needs to be idempotent as ACKs are best effort.
def pub_sub_callback(message):
    global running
    running = True
    message_data = message["data"].decode() if not using_pubsub else message.data
    global shutdown_flag
    if not shutdown_flag:
        try:
            data = json.loads(message_data)
            logging.info(f"entry: {data}")
            if data["action"] == "evaluate":
                process = subprocess.Popen(
                    ["venv/bin/python3", "evaluate_dataset.py", message_data],
                )
                if not using_pubsub:
                    process.wait()
            elif data["action"] == "refresh_scores":
                process = subprocess.Popen(
                    ["venv/bin/python3", "refresh_scores.py", message_data],
                )
                # TODO: What does the on prem wait do?
                if not using_pubsub:
                    process.wait()
        except json.decoder.JSONDecodeError:
            logging.error(f"Error parsing message: {message_data}")
        except:
            logging.error(f"Unrecognised error in message: {message_data}")
        finally:
            # acknowledge that data has been processed
            # NOTE: If the message is not acknowledged in time, pubsub will send it
            # again. To avoid this the AckDeadline can be modified.
            # The processing of the data should be as quick as possible tho.
            # (i.e. spawning a subprocess).
            # NOTE: If there is an error with the message format, it should be
            # logged and acknowledged, otherwise the message will keep coming.
            if using_pubsub:
                message.ack()
    elif using_pubsub:
        message.nack()
    running = False


def push_msg_to_queue(message):
    q.put(message)


# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

if __name__ == "__main__":
    logging.info("Starting service!")
    # Notify systemd that the service is ready
    n.notify("READY=1")

    if not using_pubsub:
        r = redis.Redis(host=os.environ.get("REDIS_HOST", "host.docker.internal"))
        p = r.pubsub()
        p.subscribe(**{subscription_name.split("/")[-1]: push_msg_to_queue})
        thread = p.run_in_thread(sleep_time=0.001)
    else:
        # This method requires either gcloud to be authenticated (this is the case
        # when using a service account in a Compute Engine instance) or to have an
        # env var GOOGLE_APPLICATION_CREDENTIALS=<path_to_credentials.json> defined
        subscriber = pubsub_v1.SubscriberClient()
        future = subscriber.subscribe(subscription_name, pub_sub_callback)

    logging.info("Subscribed to topic.")

    while not shutdown_flag:
        try:
            if using_pubsub:
                future.result(timeout=10)
            elif not q.empty() and not running:
                pub_sub_callback(q.get())
        except concurrent.futures.TimeoutError:
            logging.info("No message received")
        finally:
            n.notify("WATCHDOG=1")

    # NOTE: Any message being processed will finish before termination.
    logging.info("All tasks finished correctly! Stopping the service.")
    logging.info("Cancelling subscription...")

    if not using_pubsub:
        thread.stop()
    else:
        future.cancel()
        subscriber.close()

    n.notify("STOPPING=1")
    sys.exit(0)
