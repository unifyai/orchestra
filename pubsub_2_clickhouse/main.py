import logging
import time

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

if __name__ == "__main__":
    while True:
        logging.debug(f"ping time: ({time.time()})")
        time.sleep(5)
