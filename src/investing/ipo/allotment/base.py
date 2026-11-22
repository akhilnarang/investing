import abc
import asyncio
import logging
import re
from collections.abc import Callable
from typing import Any, ClassVar

import httpx

from ..api_schemas import (
    AllotmentIssue,
    AllotmentLookupResult,
    AllotmentOutcome,
    AllotmentRecord,
    ProviderName,
)

logger = logging.getLogger(__name__)

ClientFactory = Callable[[], httpx.AsyncClient]


class ProviderError(Exception):
    """Base class for registrar adapter failures."""


class ProviderUnavailable(ProviderError):
    """The registrar could not be reached or returned a server error."""

    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderRateLimited(ProviderUnavailable):
    """The registrar asked us to slow down."""


class UpstreamChanged(ProviderError):
    """The registrar response no longer matches the adapter."""


class UpstreamResponseError(ProviderError):
    """The registrar returned an explicit error."""


class InvalidPan(ValueError):
    """The supplied PAN format is invalid."""


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 4
# KFintech's JavaScript bundle is the largest legitimate reply.
MAX_RESPONSE_BYTES = 16_000_000
_BODY_KWARGS = ("content", "json", "data")
# aiter_bytes() has already decoded the body, so these headers no longer describe it.
_STALE_BODY_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})
_INTEGER_PATTERN = re.compile(r"^[+-]?\d+$")
_DECIMAL_PATTERN = re.compile(r"^[+-]?\d+\.\d+$")
_RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})
_PENDING_ALLOTMENT_MARKERS = (
    "awaiting",
    "not finalized",
    "not finalised",
    "not confirmed",
    "not yet",
    "pending",
    "processing",
    "under process",
)
_NEGATIVE_ALLOTMENT_MARKERS = (
    "no allotment",
    "not allot",
    "not successful",
    "rejected",
    "unsuccessful",
)
_POSITIVE_ALLOTMENT_MARKERS = (
    "allotted",
    "confirmed",
    "successful",
)


def retry_after_seconds(response: httpx.Response) -> int | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = int(float(value.strip()))
    except ValueError:
        return None
    return max(seconds, 0)


def parse_int(value: Any) -> int | None:
    """Parse a registrar integer. Accepts ``"50.0"``, rejects ``"50.5"`` rather than rounding."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    if _INTEGER_PATTERN.fullmatch(text):
        return int(text)
    if _DECIMAL_PATTERN.fullmatch(text):
        number = float(text)
        if number.is_integer():
            return int(number)
    return None


def outcome_from_allotment_text(value: str | None) -> AllotmentOutcome:
    """Interpret registrar status text without treating any mention of "allot" as confirmed."""

    text = " ".join((value or "").lower().split())
    if not text:
        return AllotmentOutcome.PENDING
    if any(marker in text for marker in _PENDING_ALLOTMENT_MARKERS):
        return AllotmentOutcome.PENDING
    if any(marker in text for marker in _NEGATIVE_ALLOTMENT_MARKERS):
        return AllotmentOutcome.NOT_ALLOTTED
    if text in {"y", "yes"} or any(
        marker in text for marker in _POSITIVE_ALLOTMENT_MARKERS
    ):
        return AllotmentOutcome.ALLOTTED
    return AllotmentOutcome.PENDING


def clean_text(value: Any) -> str | None:
    """Collapse whitespace; return None for a missing or blank value."""

    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def outcome_of_record(record: AllotmentRecord) -> AllotmentOutcome:
    """Decide an outcome from the allotted quantity, else from the status text."""

    if record.allotted_shares is not None:
        return (
            AllotmentOutcome.ALLOTTED
            if record.allotted_shares > 0
            else AllotmentOutcome.NOT_ALLOTTED
        )
    return outcome_from_allotment_text(record.remarks)


def make_result(
    outcome: AllotmentOutcome,
    issue: AllotmentIssue,
    message: str,
    *,
    records: list[AllotmentRecord] | None = None,
    allocated_shares: int | None = None,
    retry_after: int | None = None,
) -> AllotmentLookupResult:
    """Build the normalized result shared by all registrar adapters."""

    return AllotmentLookupResult(
        outcome=outcome,
        provider=issue.provider,
        issue=issue,
        message=message,
        records=records or [],
        allocated_shares=allocated_shares,
        retry_after_seconds=retry_after,
        status_page_url=issue.status_page_url,
    )


class AllotmentProvider(abc.ABC):
    """One registrar adapter.

    Subclasses parse and raise :class:`ProviderError`; :meth:`lookup` turns those
    into a normalized :class:`AllotmentLookupResult`.
    """

    name: ClassVar[ProviderName]
    label: ClassVar[str]
    status_page_url: ClassVar[str]
    allowed_hosts: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, client_factory: ClientFactory) -> None:
        self._client_factory = client_factory

    @abc.abstractmethod
    async def list_issues(self) -> list[AllotmentIssue]:
        """Return this registrar's current issues, or raise a provider error."""

    @abc.abstractmethod
    async def _lookup(
        self, issue: AllotmentIssue, pan: str
    ) -> AllotmentLookupResult:
        """Perform the registrar request and parse its response."""

    async def lookup(self, issue: AllotmentIssue, pan: str) -> AllotmentLookupResult:
        """Look up one applicant, turning every failure into a normalized result."""

        try:
            return await self._lookup(issue, pan)
        except ProviderRateLimited as exc:
            return self._failure(
                AllotmentOutcome.RATE_LIMITED,
                issue,
                str(exc),
                retry_after=exc.retry_after,
            )
        except UpstreamChanged as exc:
            return self._failure(AllotmentOutcome.UPSTREAM_CHANGED, issue, str(exc))
        except UpstreamResponseError as exc:
            return self._failure(AllotmentOutcome.UPSTREAM_ERROR, issue, str(exc))
        except ProviderUnavailable as exc:
            return self._failure(
                AllotmentOutcome.PROVIDER_UNAVAILABLE,
                issue,
                str(exc),
                retry_after=exc.retry_after,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "Allotment request failed: provider=%s error=%s",
                self.name.value,
                type(exc).__name__,
            )
            return self._failure(
                AllotmentOutcome.PROVIDER_UNAVAILABLE,
                issue,
                "The registrar did not respond.",
            )
        except Exception as exc:
            # A parser bug becomes a failed result. Log only the type: the
            # message could carry applicant data.
            logger.warning(
                "Allotment parser failed: provider=%s error=%s",
                self.name.value,
                type(exc).__name__,
            )
            return self._failure(
                AllotmentOutcome.UPSTREAM_ERROR,
                issue,
                "The registrar response could not be processed.",
            )

    def _failure(
        self,
        outcome: AllotmentOutcome,
        issue: AllotmentIssue,
        message: str,
        *,
        retry_after: int | None = None,
    ) -> AllotmentLookupResult:
        return make_result(
            outcome,
            issue,
            message,
            retry_after=retry_after,
        )

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        retry_transient: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send a request, keeping every redirect inside the registrar."""

        # Registrar front ends drop the first connection now and then, so
        # retry once.
        attempts = 2 if retry_transient else 1
        for attempt in range(attempts):
            last = attempt + 1 == attempts
            try:
                response = await self._request_once(client, method, url, **kwargs)
            except httpx.TransportError:
                if last:
                    raise
                await asyncio.sleep(0.1 * (attempt + 1))
                continue
            if response.status_code in _RETRYABLE_STATUSES and not last:
                await asyncio.sleep(0.1 * (attempt + 1))
                continue
            break
        self._raise_for_status(response)
        return response

    async def _request_once(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send one request and follow only HTTPS redirects within the registrar."""

        current = url
        request_kwargs = dict(kwargs)
        for _ in range(_MAX_REDIRECTS + 1):
            target = httpx.URL(current)
            if target.scheme != "https":
                raise UpstreamChanged("The registrar redirected to a non-HTTPS address.")
            if target.host not in self.allowed_hosts:
                raise UpstreamChanged(f"Unexpected registrar host: {target.host}.")
            async with client.stream(
                method, current, follow_redirects=False, **request_kwargs
            ) as response:
                if response.status_code not in _REDIRECT_STATUSES:
                    return await _buffered(response)
                location = response.headers.get("location")
                status = response.status_code
                base_url = response.url
            if not location:
                raise UpstreamChanged(
                    "The registrar returned a redirect without a location."
                )
            carries_body = any(key in request_kwargs for key in _BODY_KWARGS)
            if status in {307, 308} and carries_body:
                # Repeating the body at the new location would re-send the PAN.
                raise UpstreamChanged("The registrar redirected a lookup request.")
            current = str(base_url.join(location))
            # The resolved Location is joined against the response URL, which
            # already contains the original query parameters.
            request_kwargs.pop("params", None)
            if status == 303 or (status in {301, 302} and method.upper() != "GET"):
                method = "GET"
                for key in _BODY_KWARGS:
                    request_kwargs.pop(key, None)
                headers = dict(request_kwargs.get("headers") or {})
                request_kwargs["headers"] = {
                    key: value
                    for key, value in headers.items()
                    if key.lower() != "content-type"
                }
        raise ProviderUnavailable("The registrar returned too many redirects.")

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code == 429:
            raise ProviderRateLimited(
                "The registrar is rate limiting requests.",
                retry_after=retry_after_seconds(response),
            )
        if response.status_code >= 500:
            raise ProviderUnavailable(
                f"The registrar returned HTTP {response.status_code}.",
                retry_after=retry_after_seconds(response),
            )
        if response.status_code >= 400:
            raise UpstreamChanged(
                f"The registrar rejected the request with HTTP {response.status_code}."
            )

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamChanged(
                "The registrar returned a response that is not valid JSON."
            ) from exc


async def _buffered(response: httpx.Response) -> httpx.Response:
    """Read a streamed reply into memory, refusing one above MAX_RESPONSE_BYTES."""

    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
        raise UpstreamChanged("The registrar reply is too large.")
    buffer = bytearray()
    async for chunk in response.aiter_bytes():
        buffer.extend(chunk)
        if len(buffer) > MAX_RESPONSE_BYTES:
            raise UpstreamChanged("The registrar reply is too large.")
    return httpx.Response(
        status_code=response.status_code,
        headers=[
            (key, value)
            for key, value in response.headers.multi_items()
            if key.lower() not in _STALE_BODY_HEADERS
        ],
        content=bytes(buffer),
        request=response.request,
    )
