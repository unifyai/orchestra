import logging
import signal
import sys
import time

import sdnotify

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# Create an instance of sdnotify
n = sdnotify.SystemdNotifier()


# Function to handle graceful shutdown
def signal_handler(signal, frame):
    logging.info("Received termination signal, shutting down gracefully...")
    time.sleep(10)
    logging.info("All tasks finished correctly! Stopping the service.")
    n.notify("STOPPING=1")
    sys.exit(0)


# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

if __name__ == "__main__":
    logging.info("Starting service!")
    # Notify systemd that the service is ready
    n.notify("READY=1")
    while True:
        # Notify systemd that the service is alive
        n.notify("WATCHDOG=1")
        logging.debug(f"ping time: ({time.time()})")
        time.sleep(5)
