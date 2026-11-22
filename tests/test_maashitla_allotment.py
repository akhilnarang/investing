"""Maashitla registrar adapter contracts.

Maashitla spells the allotment field "shares_alloted".
"""

from investing.ipo.allotment.maashitla import parse_issues, parse_lookup_payload
from investing.ipo.api_schemas import AllotmentOutcome, ProviderName

ISSUE = parse_issues([{"company_id": "abc-123", "company_name": "ROBOKIDZ EDUVENTURES LIMITED"}])[0]


def test_catalogue_keys_lookup_by_company_name() -> None:
    # The search endpoint matches on the name; company_id keeps issue_id stable.
    assert ISSUE.provider is ProviderName.MAASHITLA
    assert ISSUE.issue_id == "maashitla:abc-123"
    assert ISSUE.provider_issue_id == "ROBOKIDZ EDUVENTURES LIMITED"


def test_allotted_response_reports_shares() -> None:
    result = parse_lookup_payload(
        {"name": "A APPLICANT", "shares_applied": "1200", "shares_alloted": "400"},
        ISSUE,
    )
    assert result.outcome is AllotmentOutcome.ALLOTTED
    assert result.allocated_shares == 400
    assert result.records[0].applied_shares == 1200
    assert result.records[0].name == "A APPLICANT"


def test_zero_allotment_is_not_allotted() -> None:
    result = parse_lookup_payload(
        {"name": "A APPLICANT", "shares_applied": "1200", "shares_alloted": "0"},
        ISSUE,
    )
    assert result.outcome is AllotmentOutcome.NOT_ALLOTTED
    assert result.allocated_shares is None

