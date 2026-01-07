"""
Tests for OpenAI client thread safety in the sync implementation.

Previously, there was a bug where a shared global AsyncOpenAI client's
connection pool got confused when called from different event loops via
the _run_async_in_sync bridge, causing PoolTimeout errors.

The fix: Use a sync OpenAI client (openai.OpenAI) which uses httpx.Client
with thread-safe connection pooling. No event loops are involved, so
the pool contention bug cannot occur.

This test verifies that concurrent embedding calls from multiple threads
complete successfully without hanging.
"""

import concurrent.futures
import time

import pytest


class TestSyncClientThreadSafety:
    """
    Tests verifying that the sync OpenAI client handles concurrent
    calls from multiple threads without issues.
    """

    @pytest.mark.anyio
    async def test_concurrent_embedding_calls_succeed(self):
        """
        Verify that concurrent embedding calls from multiple threads complete
        successfully.

        This test:
        1. Runs in an async test context (to match production FastAPI routes)
        2. Makes concurrent sync embedding calls from a thread pool
        3. All calls should complete without hanging or pool contention

        Before the sync refactor, this would hang with pool contention due to
        the AsyncOpenAI client being used from multiple event loops.
        After the refactor, the sync OpenAI client handles this correctly.
        """
        from orchestra.web.api.log.python2SQL.helpers import (
            OPENAI_API_KEY,
            _get_embeddings_batch,
        )

        if not OPENAI_API_KEY:
            pytest.skip("No OpenAI API key configured")

        num_concurrent_calls = 10
        texts_per_call = ["test text for embedding"] * 2

        def make_sync_embedding_call(call_id: int):
            """Make a sync embedding call and return results."""
            start = time.monotonic()
            try:
                result = _get_embeddings_batch(texts_per_call)
                duration = time.monotonic() - start
                return {
                    "call_id": call_id,
                    "success": True,
                    "duration": duration,
                    "result_count": len(result),
                }
            except Exception as e:
                duration = time.monotonic() - start
                return {
                    "call_id": call_id,
                    "success": False,
                    "duration": duration,
                    "error": str(e),
                    "error_type": type(e).__name__,
                }

        # Run multiple sync calls concurrently from different threads
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=num_concurrent_calls,
        )
        try:
            futures = [
                executor.submit(make_sync_embedding_call, i)
                for i in range(num_concurrent_calls)
            ]

            # Wait with a timeout - sync client should complete all calls
            results = []
            try:
                for future in concurrent.futures.as_completed(futures, timeout=30):
                    results.append(future.result())
            except concurrent.futures.TimeoutError:
                completed = len(results)
                for f in futures:
                    f.cancel()
                pytest.fail(
                    f"Timeout: Only {completed}/{num_concurrent_calls} "
                    f"calls completed within 30 seconds.",
                )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        failed = [r for r in results if not r["success"]]
        if failed:
            for f in failed:
                print(
                    f"Call {f['call_id']} failed after {f['duration']:.2f}s: "
                    f"{f['error_type']}: {f['error']}",
                )

        assert len(failed) == 0, (
            f"{len(failed)}/{num_concurrent_calls} calls failed. " f"Failures: {failed}"
        )

    def test_sync_client_is_shared_across_threads(self):
        """
        Verify that the sync OpenAI client is properly shared and reused
        across multiple threads (single global instance).
        """
        from orchestra.web.api.log.python2SQL.helpers import (
            OPENAI_API_KEY,
            _get_openai_client,
        )

        if not OPENAI_API_KEY:
            pytest.skip("No OpenAI API key configured")

        observed_client_ids = []

        def get_client_id():
            client = _get_openai_client()
            return id(client)

        # Get client IDs from multiple threads
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(get_client_id) for _ in range(10)]
            for future in concurrent.futures.as_completed(futures):
                observed_client_ids.append(future.result())

        # All threads should get the same client instance
        unique_ids = set(observed_client_ids)
        assert len(unique_ids) == 1, (
            f"Expected single shared client, but got {len(unique_ids)} different "
            f"client instances: {unique_ids}"
        )
