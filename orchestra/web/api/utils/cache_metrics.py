import json
import os
from datetime import datetime, timedelta


from orchestra.db.dao.benchmark_run_dao import BenchmarkRunDAO
from orchestra.db.dao.endpoint_dao import EndpointDAO


def _format_data(endpoint_data):
    ret = {}
    for d in endpoint_data:
        ret[d.metric_name] = float(d.value)
    ret["measured_at"] = str(d.measured_at)
    return ret


def refresh_cache(
    endpoint_dao: EndpointDAO, benchmark_dao: BenchmarkRunDAO, cache_path: str
):
    """Pulls and replaces the file in cache_path."""

    REFRESH_INTERVAL = 3 * 60 * 60

    old_cache = {}
    if os.path.isfile(cache_path):
        with open(cache_path) as f:
            old_cache = json.load(f)
            cache_date = datetime.strptime(
                old_cache["oldest_entry"], "%Y-%m-%d %H:%M:%S.%f"
            )
        if (datetime.now() - cache_date) <= timedelta(seconds=REFRESH_INTERVAL):
            return

    new_cache = {}
    for endpoint_id in endpoint_dao.get_active_endpoints():
        if old_cache and endpoint_id in old_cache:
            # TODO: timezones ???
            cache_date = datetime.strptime(
                old_cache[endpoint_id]["measured_at"], "%Y-%m-%d %H:%M:%S.%f"
            )
            if (datetime.now() - cache_date) <= timedelta(seconds=REFRESH_INTERVAL):
                new_cache[endpoint_id] = old_cache[endpoint_id]
                continue
        endpoint_data = benchmark_dao.get_latest_endpoint_benchmark(endpoint_id)
        if endpoint_data:
            new_cache[endpoint_id] = _format_data(endpoint_data)

    oldest_entry = max(e["measured_at"] for e in new_cache.values())
    new_cache["oldest_entry"] = oldest_entry
    # TODO: use tempfile module
    cache_tmp_path = cache_path.replace(".json", "_tmp.json")
    with open(cache_tmp_path, "w") as f:
        json.dump(new_cache, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(cache_tmp_path, cache_path)


if __name__ == "__main__":
    CACHE_FILE_PATH = "tmp_cache.json"
    cache_metrics(CACHE_FILE_PATH)
