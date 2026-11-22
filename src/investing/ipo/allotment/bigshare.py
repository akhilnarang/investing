import asyncio
import json
from typing import Any

from ..api_schemas import (
    AllotmentIssue,
    AllotmentLookupResult,
    AllotmentOutcome,
    AllotmentRecord,
    ProviderName,
)
from .base import (
    AllotmentProvider,
    UpstreamChanged,
    make_result,
    outcome_of_record,
    parse_int,
)
from .html import select_options

# Bigshare serves issues from three hosts; ipo1 and ipo2 mirror each other. The status form has no
# CAPTCHA token.
_HOSTS = ("ipo.bigshareonline.com", "ipo1.bigshareonline.com", "ipo2.bigshareonline.com")
_DEFAULT_HOST = _HOSTS[0]


def _status_page(host: str) -> str:
    return f"https://{host}/ipo_status.html"


def _lookup_url(host: str) -> str:
    return f"https://{host}/Data.aspx/FetchIpodetails"



_STATUS_PAGE = _status_page(_DEFAULT_HOST)

# Legacy Bigshare responses put human-readable errors inside data fields
# instead of using the newer ``Status`` contract.
_SENTINELS = (
    "please enter valid pan",
    "please enter valid",
    "invalid pan",
    "no record found",
    "record not found",
    "not found",
)

_WARMING_STATUSES = {"WARMING", "PROCESSING", "INITIALIZING"}


def parse_issues(html: str, host: str = _DEFAULT_HOST) -> list[AllotmentIssue]:
    options = select_options(html, select_id="ddlCompany")
    return [_issue(value, label, host) for value, label in options if value and label]


def _issue(company_id: str, company_name: str, host: str = _DEFAULT_HOST) -> AllotmentIssue:
    # The host is part of the identity (company 9050 exists on ipo1, not ipo). The service
    # splits issue_id on the first colon, so "bigshare:ipo1:9050" arrives as "ipo1:9050".
    suffix = "" if host == _DEFAULT_HOST else f"{_host_key(host)}:"
    return AllotmentIssue(
        issue_id=f"{ProviderName.BIGSHARE.value}:{suffix}{company_id}",
        provider=ProviderName.BIGSHARE,
        provider_label="Bigshare",
        provider_issue_id=f"{suffix}{company_id}",
        company_name=company_name,
        status_page_url=_status_page(host),
    )


def _host_key(host: str) -> str:
    """'ipo1.bigshareonline.com' -> 'ipo1', the short form carried in an issue id."""
    return host.split(".")[0]


def split_issue_id(provider_issue_id: str) -> tuple[str, str]:
    """Split 'ipo1:9050' into its host and company id. A bare id means the default host."""
    key, separator, company_id = provider_issue_id.partition(":")
    if not separator:
        return _DEFAULT_HOST, provider_issue_id
    for host in _HOSTS:
        if _host_key(host) == key:
            return host, company_id
    raise UpstreamChanged(f"Unknown Bigshare host in issue id: {key}.")


def parse_lookup_payload(payload: Any, issue: AllotmentIssue) -> AllotmentLookupResult:
    data = _object(payload)
    status = (_first_text(data, ("Status", "status")) or "").upper()
    if status == "OK":
        return _ok_result(data, issue)
    if status == "NOTFOUND":
        return make_result(
            AllotmentOutcome.NOT_FOUND,
            issue,
            _message(data)
            or "Bigshare has no application for this PAN in the selected issue.",
        )
    if status == "CAPTCHA":
        return make_result(
            AllotmentOutcome.CHALLENGE_REQUIRED,
            issue,
            _message(data)
            or "Bigshare rejected the CAPTCHA answer. Request a new challenge.",
        )
    if status == "RATELIMIT":
        return make_result(
            AllotmentOutcome.RATE_LIMITED,
            issue,
            _message(data) or "Bigshare is rate limiting requests.",
        )
    if status in _WARMING_STATUSES:
        return make_result(
            AllotmentOutcome.PROVIDER_UNAVAILABLE,
            issue,
            _message(data) or "Bigshare is still preparing the allotment data.",
        )
    if status:
        raise UpstreamChanged(f"Bigshare returned an unrecognized status: {status}.")
    # Only responses without an explicit Status may fall back to legacy
    # free-text sentinels, so a Status never loses to unrelated message text.
    sentinel = _sentinel_message(data)
    if sentinel is not None:
        return make_result(AllotmentOutcome.NOT_FOUND, issue, sentinel)
    raise UpstreamChanged("Bigshare returned a lookup response without a status.")


def _ok_result(data: dict[str, Any], issue: AllotmentIssue) -> AllotmentLookupResult:
    rows = _record_rows(data)
    if not rows:
        unrecognized = any(
            key not in {"Status", "Message", "MatchCount", "ResultToken"}
            for key in data
        )
        if unrecognized:
            raise UpstreamChanged(
                "Bigshare returned an OK response without recognized applicant fields."
            )
        return make_result(
            AllotmentOutcome.NOT_FOUND,
            issue,
            _message(data)
            or "Bigshare has no application for this PAN in the selected issue.",
        )
    records = [_record(row) for row in rows]
    outcomes = [outcome_of_record(record) for record in records]
    allotted = [
        record.allotted_shares
        for record in records
        if record.allotted_shares is not None and record.allotted_shares > 0
    ]
    if AllotmentOutcome.ALLOTTED in outcomes:
        outcome = AllotmentOutcome.ALLOTTED
        message = "Bigshare shows an allotment for this PAN."
    elif outcomes and all(item == AllotmentOutcome.NOT_ALLOTTED for item in outcomes):
        outcome = AllotmentOutcome.NOT_ALLOTTED
        message = "Bigshare shows no allotment for this PAN."
    else:
        outcome = AllotmentOutcome.PENDING
        message = "Bigshare returned a record without a confirmed allotment."
    return make_result(
        outcome,
        issue,
        message,
        records=records,
        allocated_shares=sum(allotted) if allotted else None,
    )


def _object(payload: Any) -> dict[str, Any]:
    candidate: Any = payload
    if isinstance(candidate, dict) and "d" in candidate:
        candidate = candidate["d"]
    if isinstance(candidate, str):
        try:
            candidate = json.loads(candidate)
        except ValueError as exc:
            raise UpstreamChanged(
                "Bigshare returned a lookup response that is not valid JSON."
            ) from exc
    if not isinstance(candidate, dict):
        raise UpstreamChanged("Bigshare returned an unexpected response shape.")
    return candidate


def _record_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = data.get("Records")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    if any(key in data for key in ("APPLICATION_NO", "ALLOTED", "DPID", "Name")):
        return [data]
    return []


def _record(row: dict[str, Any]) -> AllotmentRecord:
    allotted_text = _first_text(row, ("ALLOTED", "Alloted", "Allotted"))
    allotted_shares = parse_int(allotted_text)
    return AllotmentRecord(
        application_no=_first_text(row, ("APPLICATION_NO", "ApplicationNo")),
        name=_first_text(row, ("Name", "NAME", "NAME1")),
        dp_id=_first_text(row, ("DPID", "DpId")),
        category=_first_text(row, ("Category", "category")),
        applied_shares=parse_int(
            _first_text(row, ("APPLIED", "Applied", "App_Shares"))
        ),
        allotted_shares=allotted_shares,
        remarks=allotted_text if allotted_shares is None else None,
    )


def _sentinel_message(data: dict[str, Any]) -> str | None:
    for value in data.values():
        if not isinstance(value, str):
            continue
        lowered = value.strip().lower()
        for sentinel in _SENTINELS:
            if sentinel in lowered:
                return value.strip()
    return None


def _message(data: dict[str, Any]) -> str | None:
    value = _first_text(data, ("Message", "message", "Msg"))
    return value


def _first_text(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        text = " ".join(str(value).split())
        if text:
            return text
    return None


class BigshareProvider(AllotmentProvider):
    name = ProviderName.BIGSHARE
    label = "Bigshare"
    status_page_url = _STATUS_PAGE
    allowed_hosts = frozenset(_HOSTS)

    async def list_issues(self) -> list[AllotmentIssue]:
        # Mirrored hosts repeat company ids; the first wins. A host failure is tolerated as
        # long as some host answered.
        async def from_host(host: str) -> list[AllotmentIssue]:
            async with self._client_factory() as client:
                response = await self._request(
                    client, "GET", _status_page(host), retry_transient=True
                )
            return parse_issues(response.text, host)

        results = await asyncio.gather(
            *(from_host(host) for host in _HOSTS), return_exceptions=True
        )
        issues: list[AllotmentIssue] = []
        seen: set[str] = set()
        answered = False
        for result in results:
            if isinstance(result, BaseException):
                continue
            answered = True
            for issue in result:
                company_id = split_issue_id(issue.provider_issue_id)[1]
                if company_id in seen:
                    continue
                seen.add(company_id)
                issues.append(issue)
        if not answered:
            raise UpstreamChanged("No Bigshare host answered its status page.")
        if not issues:
            raise UpstreamChanged("Bigshare listed no companies on its status page.")
        return issues

    async def _lookup(
        self,
        issue: AllotmentIssue,
        pan: str,
    ) -> AllotmentLookupResult:
        host, company_id = split_issue_id(issue.provider_issue_id)
        body = {
            "Applicationno": "",
            "Company": company_id,
            "SelectionType": "PN",
            "PanNo": pan,
            "txtcsdl": "",
            "txtDPID": "",
            "txtClId": "",
            "ddlType": "0",
            "lang": "en",
        }
        async with self._client_factory() as client:
            response = await self._request(
                client,
                "POST",
                _lookup_url(host),
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Accept": "application/json",
                    "Referer": _status_page(host),
                },
                json=body,
            )
        return parse_lookup_payload(self._json(response), issue)
