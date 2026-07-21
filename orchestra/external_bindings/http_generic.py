"""Reference HTTP connector for external field bindings.

Supports:
- Per-item GET/POST via ``url_template`` / ``body_template`` with ``{input}`` slots
- Optional true batch POST via ``batch_url`` + ``batch_items_key`` when the
  remote API accepts an array of items in one request

Auth: ``Authorization: Bearer <secret>`` when ``auth.secret_value`` is set,
plus any static headers from the binding (non-secret).
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from orchestra.external_bindings.types import (
    BindingItem,
    BindingResult,
    ConnectorAuth,
    WriteResult,
)

DEFAULT_CONCURRENCY = 8
DEFAULT_TIMEOUT_S = 30


class HttpGenericConnector:
    id = "http.generic"

    def batch_fetch(
        self,
        *,
        binding: dict[str, Any],
        items: list[BindingItem],
        auth: ConnectorAuth,
    ) -> list[BindingResult]:
        if not items:
            return []
        http = binding.get("http") or {}
        if http.get("batch_url"):
            return self._batch_post(http=http, items=items, auth=auth)
        return self._per_item(http=http, items=items, auth=auth, binding=binding)

    def _headers(self, auth: ConnectorAuth, http: dict[str, Any]) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "orchestra-external-bindings/1",
        }
        static = http.get("headers") or {}
        if isinstance(static, dict):
            for k, v in static.items():
                if isinstance(v, str) and "${SECRET:" not in v:
                    headers[str(k)] = str(v)
        headers.update(auth.headers or {})
        if auth.secret_value and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {auth.secret_value}"
        return headers

    def _per_item(
        self,
        *,
        http: dict[str, Any],
        items: list[BindingItem],
        auth: ConnectorAuth,
        binding: dict[str, Any],
    ) -> list[BindingResult]:
        method = str(http.get("method") or "GET").upper()
        url_template = http.get("url_template")
        if not url_template:
            return [
                BindingResult(
                    log_event_id=i.log_event_id,
                    error="http.url_template required",
                )
                for i in items
            ]
        concurrency = int(
            binding.get("batch", {}).get("concurrency") or DEFAULT_CONCURRENCY,
        )
        timeout = float(http.get("timeout_seconds") or DEFAULT_TIMEOUT_S)
        headers = self._headers(auth, http)
        json_path = http.get("response_jsonpath")

        results: dict[int, BindingResult] = {}

        def _one(item: BindingItem) -> BindingResult:
            try:
                url = _format_template(str(url_template), item.inputs)
                body = None
                if (
                    method in {"POST", "PUT", "PATCH"}
                    and http.get("body_template") is not None
                ):
                    body_obj = http["body_template"]
                    if isinstance(body_obj, str):
                        body = _format_template(body_obj, item.inputs).encode()
                    else:
                        body = json.dumps(_deep_format(body_obj, item.inputs)).encode()
                        headers.setdefault("Content-Type", "application/json")
                raw = _http_request(
                    method,
                    url,
                    headers=headers,
                    body=body,
                    timeout=timeout,
                )
                value = _extract_jsonpath(raw, json_path) if json_path else raw
                token = None
                if isinstance(raw, dict):
                    token = (
                        raw.get("etag") or raw.get("updated_at") or raw.get("revision")
                    )
                    if token is not None:
                        token = str(token)
                return BindingResult(
                    log_event_id=item.log_event_id,
                    value=value,
                    external_token=token,
                )
            except Exception as e:
                return BindingResult(log_event_id=item.log_event_id, error=repr(e))

        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = [pool.submit(_one, item) for item in items]
            for fut in as_completed(futures):
                result = fut.result()
                results[result.log_event_id] = result

        return [results[item.log_event_id] for item in items]

    def _batch_post(
        self,
        *,
        http: dict[str, Any],
        items: list[BindingItem],
        auth: ConnectorAuth,
    ) -> list[BindingResult]:
        url = str(http["batch_url"])
        items_key = str(http.get("batch_items_key") or "items")
        id_key = str(http.get("batch_id_key") or "id")
        timeout = float(http.get("timeout_seconds") or DEFAULT_TIMEOUT_S)
        headers = self._headers(auth, http)
        headers.setdefault("Content-Type", "application/json")
        payload_items = []
        for item in items:
            row = dict(item.inputs)
            row[id_key] = item.log_event_id
            payload_items.append(row)
        body = json.dumps({items_key: payload_items}).encode()
        try:
            raw = _http_request(
                "POST",
                url,
                headers=headers,
                body=body,
                timeout=timeout,
            )
        except Exception as e:
            err = repr(e)
            return [
                BindingResult(log_event_id=i.log_event_id, error=err) for i in items
            ]

        results_key = str(http.get("batch_results_key") or "results")
        json_path = http.get("response_jsonpath")
        if not isinstance(raw, dict) or results_key not in raw:
            return [
                BindingResult(
                    log_event_id=i.log_event_id,
                    error=f"batch response missing '{results_key}'",
                )
                for i in items
            ]
        by_id: dict[int, Any] = {}
        for entry in raw[results_key] or []:
            if not isinstance(entry, dict):
                continue
            rid = entry.get(id_key)
            if rid is None:
                continue
            value = entry.get("value", entry)
            if json_path:
                value = _extract_jsonpath(entry, json_path)
            by_id[int(rid)] = value

        out: list[BindingResult] = []
        for item in items:
            if item.log_event_id not in by_id:
                out.append(
                    BindingResult(
                        log_event_id=item.log_event_id,
                        error="batch response missing item",
                    ),
                )
            else:
                out.append(
                    BindingResult(
                        log_event_id=item.log_event_id,
                        value=by_id[item.log_event_id],
                    ),
                )
        return out

    def execute_write(
        self,
        *,
        binding: dict[str, Any],
        payload: dict[str, Any],
        idempotency_key: str,
        auth: ConnectorAuth,
    ) -> WriteResult:
        write = binding.get("write") or binding.get("http") or {}
        if not isinstance(write, dict):
            return WriteResult(ok=False, error="binding.write or binding.http required")
        method = str(write.get("method") or "POST").upper()
        url_template = write.get("url_template") or write.get("write_url_template")
        if not url_template:
            return WriteResult(ok=False, error="write.url_template required")
        timeout = float(write.get("timeout_seconds") or DEFAULT_TIMEOUT_S)
        headers = self._headers(auth, write)
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("Idempotency-Key", idempotency_key)
        try:
            url = _format_template(str(url_template), payload)
            body_template = write.get("body_template", payload)
            if isinstance(body_template, str):
                body = _format_template(body_template, payload).encode()
            else:
                body = json.dumps(_deep_format(body_template, payload)).encode()
            raw = _http_request(
                method,
                url,
                headers=headers,
                body=body,
                timeout=timeout,
            )
            token = None
            if isinstance(raw, dict):
                token = raw.get("etag") or raw.get("id") or raw.get("revision")
                if token is not None:
                    token = str(token)
            return WriteResult(ok=True, response=raw, external_token=token)
        except Exception as e:
            return WriteResult(ok=False, error=repr(e))


def _format_template(template: str, inputs: dict[str, Any]) -> str:
    class _Safe(dict):
        def __missing__(self, key: str) -> str:
            return ""

    return template.format_map(
        _Safe({k: "" if v is None else v for k, v in inputs.items()}),
    )


def _deep_format(obj: Any, inputs: dict[str, Any]) -> Any:
    if isinstance(obj, str):
        return _format_template(obj, inputs)
    if isinstance(obj, list):
        return [_deep_format(x, inputs) for x in obj]
    if isinstance(obj, dict):
        return {k: _deep_format(v, inputs) for k, v in obj.items()}
    return obj


def _http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: Optional[bytes],
    timeout: float,
) -> Any:
    req = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e
    except URLError as e:
        raise RuntimeError(f"URL error: {e}") from e
    if "application/json" in ctype or (raw[:1] in (b"{", b"[")):
        return json.loads(raw.decode("utf-8"))
    return raw.decode("utf-8", errors="replace")


def _extract_jsonpath(data: Any, path: Optional[str]) -> Any:
    """Minimal ``$.a.b[0]`` extractor (enough for bindings; not full JSONPath)."""
    if not path or path in ("$", ""):
        return data
    cur = data
    token = path[1:] if path.startswith("$") else path
    if token.startswith("."):
        token = token[1:]
    if not token:
        return data
    for part in token.replace("[", ".[").split("."):
        if not part:
            continue
        if part.startswith("[") and part.endswith("]"):
            idx = int(part[1:-1])
            cur = cur[idx]
        else:
            if not isinstance(cur, dict):
                raise KeyError(f"Cannot traverse '{part}' on non-object")
            cur = cur[part]
    return cur
