import json
import os
import time

from orchestra.db.dao.benchmark_run_dao import BenchmarkRunDAO


def _format_data(endpoint_data):
    return ""


def get_all_endpoints():
    """ queries the db and gets the endpoints that we care about. """
    return ""


def refresh_cache(benchmark_dao: BenchmarkRunDAO, cache_path: str):
    """ Pulls and replaces the file in cache_path. """

    REFRESH_INTERVAL = 3 * 60 * 60

    old_cache = {}
    if os.path.isfile(cache_path):
        with open(cache_path) as f:
            old_cache = json.load(f)
        if (time.time() - old_cache["oldest_entry"]) <= REFRESH_INTERVAL:
            return

    new_cache = {}
    for endpoint_id in get_all_endpoints():
        if old_cache and endpoint_id in old_cache:
            endpoint_cache_time = old_cache[endpoint_id]["benchmark_time"]
            if (time.time() - endpoint_cache_time) <= REFRESH_INTERVAL
                new_cache[endpoint_id] = old_cache[endpoint_id]
                continue

        endpoint_data = benchmark_dao.get_model_benchmark_datapoints(endpoint_id)
        new_cache[endpoint_id] = _format_data(endpoint_data)
    
    oldest_entry = max(e["benchmark_time"] for e in new_cache)
    new_cache["oldest_entry"] = oldest_entry
    # TODO: use tempfile module
    cache_tmp_path = cache_path.replace(".json", "_tmp.json")
    with open(cache_tmp_path, 'w') as f:
        json.dump(f, new_cache)
        f.flush()
        os.fsync(f.fileno())
    os.replace(cache_tmp_path, cache_path)


if __name__ == "__main__":
    CACHE_FILE_PATH = "tmp_cache.json"
    cache_metrics(CACHE_FILE_PATH)
