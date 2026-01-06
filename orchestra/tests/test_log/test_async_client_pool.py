"""
Tests for AsyncOpenAI client connection pool fix.

Verifies the fix for a bug where a shared global AsyncOpenAI client's
connection pool got confused when called from different event loops,
causing PoolTimeout errors.

The bug manifested when:
1. A global _async_client was created at module import time
2. Multiple concurrent calls went through _run_async_in_sync, each spawning
   a new event loop in a ThreadPoolExecutor
3. The httpx connection pool tracked connections per-event-loop, so connections
   "checked out" in one loop appeared stuck when accessed from another

The fix: Use per-event-loop AsyncOpenAI clients via _get_async_client().
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
    async def test_per_loop_clients_avoid_pool_contention(self):
        """
        Verify that per-event-loop clients avoid connection pool contention.

        This test:
        1. Runs in an async test (one event loop)
        2. Calls sync wrappers that create NEW event loops via ThreadPoolExecutor
        3. Each event loop gets its own AsyncOpenAI client with its own pool

        Before the fix, this would hang with pool contention.
        After the fix, all calls complete successfully.
        """
        from orchestra.web.api.log.python2SQL.helpers import (
            OPENAI_API_KEY,
            _get_embeddings_batch_sync,
        )

        if not OPENAI_API_KEY:
            pytest.skip("No OpenAI API key configured")

        # Make concurrent sync calls from this async context.
        # Each call goes through _run_async_in_sync which creates a new event loop.
        # With the fix, each loop gets its own client, avoiding contention.

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
                    f"Pool contention occurred: Only {completed}/{num_concurrent_calls} "
                    f"calls completed within 5 seconds. Per-loop clients should prevent this.",
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

        This proves why per-loop clients are needed: a shared client would be
        accessed from multiple different event loops, causing pool confusion.
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
