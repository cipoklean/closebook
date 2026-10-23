"""Airwallex sandbox HTTP client.

Hard rules implemented here (closebook.md §1):
- Auth: POST /api/v1/authentication/login (headers x-client-id + x-api-key, NO body).
  Bearer token lasts 30 min -> cached, refreshed at 25 min.
- Every mutating call carries a UUIDv4 request_id, PERSISTED before the call
  (the caller does that via `reserve_request_id`).
- Treat any non-2xx or timeout on a money movement as AMBIGUOUS: before
  retrying, look up the object by request_id. `ambiguously_submitted` is the
  guard for this.
- Rate limits: 20 req/s overall, 10/endpoint, 10 concurrent. We enforce a
  conservative client-side token bucket per endpoint and back off on 429.
- The LLM never uses this class; it only appears to human-approved, code-
  validated paths in domain/executor.py.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Optional

import httpx

from ..config import get_settings
from ..money import to_major_str

TOKEN_TTL_S = 30 * 60          # Airwallex token lifetime
REFRESH_BEFORE_S = 25 * 60     # refresh at 25 min
RATE_LIMIT_PER_S = 20         # global ceiling (10/endpoint is stricter per call-site)


class AmbiguousSubmit(Exception):
    """A money movement left the client without a confirmed result.

    Raise when a POST of a mutation returns neither a clean success nor a
    clean reject (timeout, connection error mid-flight, or 5xx). The caller
    must look the object up by request_id before retrying.
    """


class ApiError(Exception):
    def __init__(self, status: int, code: Optional[str], message: str, payload: Any = None):
        self.status = status
        self.code = code
        self.payload = payload
        super().__init__(f"[{code}] {message}" if code else message)


class RateLimiter:
    """Simple per-endpoint + global token bucket (good enough for a demo)."""

    def __init__(self, global_per_s: float = RATE_LIMIT_PER_S, per_endpoint_per_s: float = 10.0):
        self.global_tokens = global_per_s
        self.endpoint_tokens: dict[str, float] = {}
        self.last = time.monotonic()
        self.lock = threading.Lock()
        self.g = global_per_s
        self.e = per_endpoint_per_s

    def acquire(self, endpoint_key: str) -> None:
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last
            self.last = now
            self.global_tokens = min(self.g, self.global_tokens + elapsed * self.g)
            self.endpoint_tokens[endpoint_key] = min(
                self.e, self.endpoint_tokens.get(endpoint_key, 0.0) + elapsed * self.e
            )
            if self.global_tokens < 1 or self.endpoint_tokens[endpoint_key] < 1:
                wait = max(1 - self.global_tokens / self.g, 1 - self.endpoint_tokens[endpoint_key] / self.e)
                time.sleep(wait)
                now = time.monotonic()
                self.global_tokens += (now - self.last) * self.g
                self.endpoint_tokens[endpoint_key] = min(self.e, self.endpoint_tokens[endpoint_key] + (now - self.last) * self.e)
            self.global_tokens -= 1
            self.endpoint_tokens[endpoint_key] -= 1


class AirwallexClient:
    def __init__(self, settings=None, client: httpx.Client | None = None):
        self.settings = settings or get_settings()
        self._http = client or httpx.Client(
            base_url=self.settings.awx_base_url, timeout=httpx.Timeout(20.0)
        )
        self._token: Optional[str] = None
        self._token_obtained_at = 0.0
        self._token_lock = threading.Lock()
        self._limiter = RateLimiter()
        self._owns_http = client is None

    # ---- auth -------------------------------------------------------
    def _ensure_token(self) -> str:
        with self._token_lock:
            if self._token and (time.monotonic() - self._token_obtained_at) < REFRESH_BEFORE_S:
                return self._token
            resp = self._http.post(
                "/api/v1/authentication/login",
                headers={
                    "x-client-id": self.settings.awx_client_id.get_secret_value(),
                    "x-api-key": self.settings.awx_api_key.get_secret_value(),
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            payload = resp.json()
            # Sandbox login returns {"token": ..., "expires_at": ...}
            # (not "access_token"); accept either shape. Guard against a
            # non-dict body so the failure is a clear error, not an
            # AttributeError deep in the auth path.
            if not isinstance(payload, dict):
                raise ApiError(resp.status_code, None,
                               f"unexpected login response type: {type(payload).__name__}")
            token: str = payload.get("token") or payload.get("access_token")
            if not token:
                raise ApiError(resp.status_code, None, "login returned no token field")
            self._token = token
            self._token_obtained_at = time.monotonic()
            return token

    # ---- request_id ---------------------------------------------------
    @staticmethod
    def new_request_id() -> str:
        # 10-50 char constraint: a bare uuid4 hex is 32 chars (valid).
        return str(uuid.uuid4())

    # ---- core call ----------------------------------------------------
    def call(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        query: dict | None = None,
        request_id: str | None = None,
        money_amounts_minor: dict[str, tuple[int, str]] | None = None,
        on_behalf_of: str | None = None,
        is_simulation: bool = False,
    ) -> Any:
        """Make an authenticated call.

        money_amounts_minor: {json_field_path: (minor_units, currency)} — the
        boundary where major-unit conversion happens, and only here.
        """
        ep_key = f"{method} {path}"
        self._limiter.acquire(ep_key)

        headers = {"Authorization": f"Bearer {self._ensure_token()}"}
        if on_behalf_of:
            headers["x-on-behalf-of"] = on_behalf_of

        if json_body is not None:
            body = json_body
            if request_id:
                body = {**body, "request_id": request_id}
            for dotted, (minor, ccy) in (money_amounts_minor or {}).items():
                self._set_money_field(body, dotted, to_major_str(minor, ccy))
        else:
            body = None

        last_problem = "no response"
        for attempt in range(4):
            try:
                resp = self._http.request(
                    method, path, json=body, params=query, headers=headers, timeout=20.0
                )
            except httpx.TimeoutException:
                last_problem = "the request timed out"
                if method.upper() in {"POST"} and not is_simulation:
                    raise AmbiguousSubmit(
                        f"{path} timed out mid-flight: look up by request_id={request_id} before retrying"
                    ) from None
                continue
            except httpx.ConnectError as exc:
                last_problem = f"could not connect: {exc}"
                continue

            if resp.status_code == 429:
                last_problem = "HTTP 429 (rate limited)"
                retry_after = float(resp.headers.get("retry-after", 1.0))
                time.sleep(min(retry_after, 2.0))
                continue

            if resp.status_code < 500:
                data = self._safe_json(resp)
                if resp.status_code >= 400:
                    raise ApiError(
                        resp.status_code,
                        (data or {}).get("code") if isinstance(data, dict) else None,
                        (data or {}).get("message", resp.text) if isinstance(data, dict) else resp.text,
                        data,
                    )
                return data

            # 5xx on a non-simulation mutation = ambiguous
            if method.upper() == "POST" and not is_simulation:
                raise AmbiguousSubmit(f"5xx from {path}: look up by request_id={request_id}")
            # Remember WHY, so exhausting the retries can say what came back.
            # This used to report every exhaustion as "rate-limited", which sent
            # a 500-looking bug hunting for a throttle that was never there.
            last_problem = f"HTTP {resp.status_code}: {resp.text[:200]}"
            time.sleep(0.5 * (attempt + 1))

        raise ApiError(0, None,
                       f"gave up after 4 attempts on {path}, {last_problem}")

    @staticmethod
    def _safe_json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except Exception:
            return None

    @staticmethod
    def _set_money_field(d: Any, dotted: str, value: str) -> None:
        parts = dotted.split(".")
        node = d
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value

    def close(self) -> None:
        if self._owns_http:
            self._http.close()


def _reserve_request_id(db_session, plan_step) -> str:
    """Persist a fresh request_id on the plan_step BEFORE the call and return it.

    If the step already has one (a prior in-flight attempt), REUSE it — that is
    exactly the same-operation retry path Airwallex's 7-day dup window expects.

    With no plan_step (a one-off call such as creating a beneficiary from a
    script) there is nowhere to persist an id, so mint a fresh one. That is
    safe because such calls are not retried automatically — the retry path
    that actually matters always has a row.
    """
    from sqlalchemy import update

    if plan_step is None:
        return AirwallexClient.new_request_id()

    if not plan_step.request_id:
        # CLAIM IT CONDITIONALLY, THEN RE-READ. Two cycles can both find the
        # column empty and both mint a key; only one UPDATE can win, because
        # `request_id IS NULL` is part of the WHERE. The loser then re-reads the
        # winner's value instead of carrying its own, so both cycles leave with
        # the SAME key -- and the same key is what makes Airwallex (and our own
        # lookup) treat the second attempt as the first one. Minting
        # unconditionally meant two transfers for one approval.
        rid = AirwallexClient.new_request_id()
        result = db_session.execute(
            update(type(plan_step))
            .where(type(plan_step).id == plan_step.id,
                   type(plan_step).request_id.is_(None))
            .values(request_id=rid)
        )
        db_session.flush()
        if result.rowcount:
            plan_step.request_id = rid
        else:
            # Somebody else claimed it between our read and our write. Take
            # theirs: the pointer on the row is the single source of truth.
            db_session.expire(plan_step, ["request_id"])
            db_session.refresh(plan_step)
    return plan_step.request_id


__all__ = ["AirwallexClient", "ApiError", "AmbiguousSubmit", "_reserve_request_id"]
