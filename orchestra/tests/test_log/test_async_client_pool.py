"""
Tests for AsyncOpenAI client connection pool issues when used across event loops.

This test reproduces the issue where a shared global AsyncOpenAI client's
connection pool gets confused when called from different event loops,
causing PoolTimeout errors.

The issue manifests when:
1. The global _async_client is created at module import time
2. Multiple concurrent calls go through _run_async_in_sync, each spawning
   a new event loop in a ThreadPoolExecutor
3. The httpx connection pool tracks connections per-event-loop, so connections
   "checked out" in one loop appear stuck when accessed from another

The symptom is: some embedding requests hang indefinitely waiting for a
connection from the pool, eventually timing out with httpcore.PoolTimeout.
"""

import asyncio
import concurrent.futures
import time

import pytest


class TestAsyncClientPoolContention:
    """
    Tests demonstrating connection pool contention when a shared AsyncOpenAI
    client is used from multiple event loops.
    """

    @pytest.mark.anyio
    async def test_shared_async_client_pool_contention(self):
        """
        Reproduce the connection pool issue by:
        1. Running in an async test (one event loop)
        2. Calling sync wrappers that create NEW event loops via ThreadPoolExecutor
        3. Multiple concurrent calls cause pool contention due to shared client
        """
        from orchestra.web.api.log.python2SQL.helpers import (
            _async_client,
            _get_embeddings_batch_sync,
        )

        # Skip if no OpenAI API key configured
        if _async_client is None:
            pytest.skip("No OpenAI API key configured")

        # We'll make concurrent sync calls from this async context.
        # Each call goes through _run_async_in_sync which creates a new event loop.
        # The shared _async_client causes pool contention.

        num_concurrent_calls = 10
        texts_per_call = ["test text for embedding"] * 2

        def make_sync_embedding_call(call_id: int):
            """Simulate a sync context calling the embedding function."""
            start = time.monotonic()
            try:
                result = _get_embeddings_batch_sync(texts_per_call)
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

            # Wait with a short timeout - if pool contention occurs, calls will hang
            results = []
            try:
                for future in concurrent.futures.as_completed(futures, timeout=5):
                    results.append(future.result())
            except concurrent.futures.TimeoutError:
                completed = len(results)
                for f in futures:
                    f.cancel()
                pytest.fail(
                    f"POOL CONTENTION BUG: Only {completed}/{num_concurrent_calls} "
                    f"calls completed within 5 seconds. The remaining calls are stuck "
                    f"waiting for connections from the shared AsyncClient's pool.",
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

    @pytest.mark.anyio
    async def test_demonstrates_event_loop_mismatch(self):
        """
        Minimal test demonstrating that _run_async_in_sync creates different
        event loops when called from an async context.

        This proves that the global _async_client will be accessed from
        multiple different event loops, which is the root cause of the
        connection pool issue.
        """
        from orchestra.web.api.log.python2SQL.helpers import _run_async_in_sync

        test_loop = asyncio.get_running_loop()
        test_loop_id = id(test_loop)

        observed_loop_ids = []

        async def capture_loop_id():
            loop = asyncio.get_running_loop()
            return id(loop)

        for _ in range(3):
            loop_id = _run_async_in_sync(capture_loop_id())
            observed_loop_ids.append(loop_id)

        print(f"Test event loop: {test_loop_id}")
        print(f"Observed loop IDs from _run_async_in_sync: {observed_loop_ids}")

        for loop_id in observed_loop_ids:
            assert loop_id != test_loop_id, (
                "_run_async_in_sync should create a new event loop when called "
                "from an async context, but got the same loop as the test"
            )

        print(
            f"\nDemonstrated: {len(observed_loop_ids)} calls to _run_async_in_sync "
            f"all used event loops different from the test's loop ({test_loop_id})",
        )
