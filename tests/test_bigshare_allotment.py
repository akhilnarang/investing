"""Bigshare registrar adapter contracts, offline, through an httpx mock transport."""
import asyncio
import json
import logging

import httpx

from investing.ipo.allotment.bigshare import (
    _DEFAULT_HOST,
    BigshareProvider,
    _issue,
    split_issue_id,
)
from investing.ipo.api_schemas import AllotmentOutcome

FAKE_PAN = "AAAAA9999A"


def test_bigshare_issue_id_carries_its_host():
    """Bigshare runs three status pages; an issue id names the host it lives on."""
    default = _issue("9049", "JINDAL SUPREME (INDIA) LIMITED")
    assert default.issue_id == "bigshare:9049", "the default host keeps the original id format"
    assert split_issue_id(default.provider_issue_id) == (_DEFAULT_HOST, "9049")

    other = _issue("9050", "A-ONE STEELS INDIA LIMITED", "ipo1.bigshareonline.com")
    assert other.issue_id == "bigshare:ipo1:9050"
    assert split_issue_id(other.provider_issue_id) == ("ipo1.bigshareonline.com", "9050")
    assert other.status_page_url == "https://ipo1.bigshareonline.com/ipo_status.html"

    # A bare id resolves to the default host.
    assert split_issue_id("9049") == (_DEFAULT_HOST, "9049")


def test_bigshare_lookup_sends_no_captcha_fields(caplog):
    """Bigshare has no CAPTCHA; the lookup posts no token fields."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200,
            json={
                "d": {
                    "Status": "OK",
                    "Name": "A APPLICANT",
                    "APPLIED": "1200",
                    "ALLOTED": "400",
                }
            },
        )

    transport = httpx.MockTransport(handler)
    provider = BigshareProvider(lambda: httpx.AsyncClient(transport=transport))
    issue = _issue("9050", "A-ONE STEELS INDIA LIMITED", "ipo1.bigshareonline.com")

    caplog.set_level(logging.INFO)
    result = asyncio.run(provider.lookup(issue, FAKE_PAN))

    # A parsed outcome shows the request below went through the real path.
    assert result.outcome is AllotmentOutcome.ALLOTTED
    request = sent[0]
    posted = json.loads(request.content)
    assert set(posted) & {"CaptchaToken", "CaptchaAnswer", "ResultToken"} == set()
    # Posts the bare company id, not "ipo1:9050".
    assert posted["Company"] == "9050"
    assert posted["PanNo"] == FAKE_PAN
    assert posted["SelectionType"] == "PN"
    assert str(request.url) == "https://ipo1.bigshareonline.com/Data.aspx/FetchIpodetails"
    assert FAKE_PAN not in caplog.text, "a PAN must never reach a log line"
