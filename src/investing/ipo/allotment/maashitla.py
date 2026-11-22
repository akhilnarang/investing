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
    UpstreamResponseError,
    clean_text,
    make_result,
    outcome_of_record,
    parse_int,
)

_STATUS_PAGE = "https://maashitla.com/allotment-status/public-issues"
_API_BASE = "https://api.maashitla.com/api"
_COMPANIES_URL = f"{_API_BASE}/public-issue/companies"
_SEARCH_URL = f"{_API_BASE}/public-issue/search"


def parse_issues(payload: Any) -> list[AllotmentIssue]:
    """Read the ``[{company_id, company_name}]`` catalogue."""

    if not isinstance(payload, list):
        raise UpstreamChanged("Maashitla returned a company list that is not an array.")
    issues: list[AllotmentIssue] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        name = " ".join(str(row.get("company_name") or "").split())
        if not name:
            continue
        # The search endpoint keys off the company name, not company_id, so the
        # name is what has to survive as the provider issue id.
        issues.append(_issue(name, row.get("company_id")))
    if not issues:
        raise UpstreamChanged("Maashitla listed no companies for public issues.")
    return issues


def _issue(company_name: str, company_id: Any = None) -> AllotmentIssue:
    # company_id is stable across renames, so it makes the better issue_id
    # suffix when Maashitla supplies one.
    key = " ".join(str(company_id).split()) if company_id else company_name
    return AllotmentIssue(
        issue_id=f"{ProviderName.MAASHITLA.value}:{key}",
        provider=ProviderName.MAASHITLA,
        provider_label="Maashitla Securities",
        provider_issue_id=company_name,
        company_name=company_name,
        status_page_url=_STATUS_PAGE,
    )


def parse_lookup_payload(payload: Any, issue: AllotmentIssue) -> AllotmentLookupResult:
    """Normalize the flat applicant object Maashitla returns for a match."""

    row = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(row, dict):
        raise UpstreamChanged("Maashitla returned an unexpected lookup response shape.")
    if not any(field in row for field in ("name", "shares_applied", "shares_alloted")):
        detail = row.get("detail")
        if isinstance(detail, str) and detail.strip():
            raise UpstreamResponseError(f"Maashitla rejected the lookup: {detail}")
        raise UpstreamChanged("Maashitla returned a lookup response without applicant fields.")

    # Maashitla spells the field "alloted"; accept both spellings.
    allotted_text = clean_text(row.get("shares_alloted", row.get("shares_allotted")))
    allotted_shares = parse_int(allotted_text)
    record = AllotmentRecord(
        application_no=clean_text(row.get("application_no")),
        name=clean_text(row.get("name")),
        dp_id=clean_text(row.get("dpid_client_id")),
        category=clean_text(row.get("category")),
        applied_shares=parse_int(clean_text(row.get("shares_applied"))),
        allotted_shares=allotted_shares,
        remarks=allotted_text if allotted_shares is None else None,
    )
    outcome = outcome_of_record(record)
    if outcome is AllotmentOutcome.ALLOTTED:
        message = "Maashitla Securities shows an allotment for this PAN."
    elif outcome is AllotmentOutcome.NOT_ALLOTTED:
        message = "Maashitla Securities shows no allotment for this PAN."
    else:
        message = "Maashitla Securities returned a record without a confirmed allotment."
    return make_result(
        outcome,
        issue,
        message,
        records=[record],
        allocated_shares=allotted_shares if outcome is AllotmentOutcome.ALLOTTED else None,
    )


class MaashitlaProvider(AllotmentProvider):
    name = ProviderName.MAASHITLA
    label = "Maashitla Securities"
    status_page_url = _STATUS_PAGE
    allowed_hosts = frozenset({"api.maashitla.com", "maashitla.com"})

    async def list_issues(self) -> list[AllotmentIssue]:
        async with self._client_factory() as client:
            response = await self._request(
                client,
                "GET",
                _COMPANIES_URL,
                headers={"Accept": "application/json", "Referer": _STATUS_PAGE},
                retry_transient=True,
            )
        return parse_issues(self._json(response))

    async def _lookup(
        self,
        issue: AllotmentIssue,
        pan: str,
    ) -> AllotmentLookupResult:
        # Maashitla signals "no application" with HTTP 404, which the shared
        # _request turns into UpstreamChanged, so the lookup is sent directly
        # and only this status is read before the usual status checks.
        async with self._client_factory() as client:
            response = await self._request_once(
                client,
                "GET",
                _SEARCH_URL,
                params={"company_name": issue.provider_issue_id, "pan": pan},
                headers={"Accept": "application/json", "Referer": _STATUS_PAGE},
            )
        if response.status_code == 404:
            return make_result(
                AllotmentOutcome.NOT_FOUND,
                issue,
                "Maashitla Securities has no application for this PAN in the selected issue.",
            )
        self._raise_for_status(response)
        return parse_lookup_payload(self._json(response), issue)
