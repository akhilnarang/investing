"""Download every Zerodha Console mutual-fund tradebook from a start date to today, one financial year at a time.

Uses your logged-in Console session against an undocumented endpoint, which can change without notice.
Console serves one financial year per download and can answer with an earlier window's report, so each
report's trade dates are checked against the window. Years already on disk are skipped if their dates fit;
--force refetches everything.

    coin-tradebook --since 2019-04-01            # asks for the cookie and token at hidden prompts
    coin-tradebook --since 2019-04-01 --merge    # also one deduplicated tradebook-all.csv
    CONSOLE_COOKIE=... CONSOLE_CSRF=... coin-tradebook --since 2019-04-01   # non-interactive
    printf '%s\n%s\n' "$COOKIE" "$CSRF" | coin-tradebook --since 2019-04-01 --stdin

Log in to https://console.zerodha.com, open the browser's developer tools on the Network tab, download any
report from Reports > Tradebook, and copy that request's `cookie` and `x-csrftoken` headers. Give them at the
hidden prompts, as CONSOLE_COOKIE / CONSOLE_CSRF, or on stdin with --stdin. There is no argument for them, to
keep them out of shell history and the process list. They are sent to console.zerodha.com only.

Files are written to ./tradebooks as tradebook-FY2019-20.csv and so on; years with no trades are skipped.
`--segment EQ` downloads the stock tradebook as tradebook-eq-FY2019-20.csv.
"""
import argparse
import csv
import hashlib
import io
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

from . import config
from .config import USER_AGENT
from .core.clock import today_ist

URL = "https://console.zerodha.com/api/download/tradebook"
REFERER = "https://console.zerodha.com/reports/tradebook"
TRADEBOOK_COLUMNS = ["symbol", "isin", "trade_date", "exchange", "segment", "series", "trade_type", "auction", "quantity",
                     "price", "trade_id", "order_id", "order_execution_time"]


def financial_years(since: date, until: date):
    """(label, from, to) per financial year (1 April to 31 March), clipped to since..until."""
    start = since
    while start <= until:
        fy_start = date(start.year if start.month >= 4 else start.year - 1, 4, 1)
        fy_end = date(fy_start.year + 1, 3, 31)
        label = f"FY{fy_start.year}-{str(fy_start.year + 1)[-2:]}"
        if start != fy_start:
            label += f"-from-{start.isoformat()}"   # a clipped first window
        yield label, start, min(fy_end, until)
        start = fy_end + timedelta(days=1)


def looks_like_tradebook(text: str) -> bool:
    head = text.lstrip("\ufeff").splitlines()[:1]
    return bool(head) and "trade_id" in head[0] and "isin" in head[0]


def rows_to_csv(rows: list[dict]) -> str:
    """Console sometimes answers with the rows as JSON; write them in the tradebook's own column order."""
    extra = [k for r in rows for k in r if k not in TRADEBOOK_COLUMNS]
    cols = TRADEBOOK_COLUMNS + sorted(set(extra))
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


seen_reports: set[str] = set()   # sha256 of every report accepted in this run


def show_response(r: httpx.Response, frm: date, to: date, attempt: int) -> None:
    """Print a JSON answer's status and job state to stderr. The body is not printed: it can hold a signed link."""
    ctype = r.headers.get("content-type", "")
    try:
        body = r.json() if ("json" in ctype or r.text[:1] == "{") else None
    except ValueError:
        body = None
    if isinstance(body, dict):
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        state = data.get("state") or body.get("status") or body.get("error_type") or ""
        print(f"  {frm}..{to} attempt {attempt}: HTTP {r.status_code} {state}".rstrip(), file=sys.stderr)


def get_following(client: httpx.Client, url: str, params: dict | None = None) -> httpx.Response:
    """A GET that follows redirects by hand, sending the session headers only to Console's host."""
    same = httpx.URL(url).host in ("", httpx.URL(URL).host)
    r = client.get(url, params=params) if same else httpx.get(url, headers={"user-agent": USER_AGENT}, timeout=90, follow_redirects=False)
    hops = 0
    while r.is_redirect and hops < 5:
        hops += 1
        target = str(r.next_request.url) if r.next_request else str(httpx.URL(url).join(r.headers.get("location", "")))
        same = httpx.URL(target).host in ("", httpx.URL(URL).host)
        r = client.get(target) if same else httpx.get(target, headers={"user-agent": USER_AGENT}, timeout=90, follow_redirects=False)
    return r


def fetch_window(client: httpx.Client, frm: date, to: date, segment: str, poll: float = 5.0, wait: float = 240.0) -> str:
    """One financial year. A new report answers {"data": {"state": "PENDING"}} and the same URL is polled."""
    deadline = time.time() + wait
    attempt = 0
    while True:
        attempt += 1
        params = {"segment": segment, "tradingsymbol": "", "from_date": frm.isoformat(), "to_date": to.isoformat(), "format": "csv"}
        r = get_following(client, URL, params)
        show_response(r, frm, to, attempt)
        if r.status_code in (401, 403):
            raise SystemExit(f"Console answered {r.status_code}: the cookie or x-csrftoken is missing or expired; copy fresh ones")
        if r.status_code == 429:
            raise SystemExit("Console answered 429 (too many requests): wait a few minutes and rerun with a longer --pause")
        r.raise_for_status()
        text = r.text.lstrip("\ufeff")
        if "json" in r.headers.get("content-type", "") or text[:1] == "{":
            payload = r.json()
            data = (payload.get("data") if isinstance(payload, dict) else None) or {}
            if isinstance(data, dict) and data.get("url"):
                r2 = get_following(client, data["url"])
                r2.raise_for_status()
                text = r2.text.lstrip("\ufeff")
            elif isinstance(data, dict) and str(data.get("state", "")).upper() in ("PENDING", "QUEUED", "PROCESSING", "RUNNING"):
                if time.time() > deadline:
                    raise SystemExit(f"Console is still building the {frm}..{to} report after {wait:.0f}s; rerun later, it will be ready")
                print(f"  {frm}..{to}: Console is generating the report, waiting {poll:.0f}s (attempt {attempt})", flush=True)
                time.sleep(poll)
                continue
            elif isinstance(data, dict) and isinstance(data.get("result"), list):
                if not data["result"]:
                    return ",".join(TRADEBOOK_COLUMNS) + "\n"   # no trades that year
                text = rows_to_csv(data["result"])
            else:
                raise SystemExit(f"unexpected JSON from Console for {frm}..{to}: {str(payload)[:300]}")
        if "<html" in text[:500].lower():
            raise SystemExit("Console returned a web page instead of the report: the session has expired, log in again and copy fresh headers")
        if not looks_like_tradebook(text):
            raise SystemExit(f"the response for {frm}..{to} is not a tradebook CSV; first line: {text.splitlines()[:1]}")
        digest = hashlib.sha256(text.encode()).hexdigest()
        if rows_in(text) == 0:
            return text   # every empty year has the same digest
        if not within(text, frm, to, segment) or digest in seen_reports:
            ds = dates_in(text)
            why = "the same report as an earlier year" if digest in seen_reports else (f"trades dated {ds[0]}..{ds[-1]}" if ds else "unreadable trade dates")
            if time.time() > deadline:
                raise SystemExit(f"Console keeps answering the {frm}..{to} request with {why}; rerun later with --force")
            print(f"  {frm}..{to}: Console answered with {why}, not this window; waiting {poll:.0f}s (attempt {attempt})", flush=True)
            time.sleep(poll)
            continue
        seen_reports.add(digest)
        return text


def rows_in(text: str) -> int:
    return sum(1 for _ in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))


def dates_in(text: str) -> list[str]:
    return sorted((r.get("trade_date") or "")[:10] for r in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))


def within(text: str, frm: date, to: date, segment: str) -> bool:
    """Every trade date parses and lies in the window, and every row is in the segment."""
    for r in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        try:
            d = date.fromisoformat((r.get("trade_date") or "")[:10])
        except ValueError:
            return False
        if not (frm <= d <= to):
            return False
        seg = (r.get("segment") or segment).strip().upper()
        if segment == "MF" and seg not in ("MF", "MUTUAL FUNDS", "MUTUAL FUND"):
            return False
        if segment == "EQ" and seg not in ("EQ", "EQUITY", "CASH"):
            return False
    return True


def merge(paths: list[Path], out: Path) -> int:
    seen, rows, header = set(), [], []
    for p in sorted(paths):
        with p.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for col in reader.fieldnames or []:
                if col not in header:
                    header.append(col)
            for row in reader:
                key = tuple((row.get(k) or "").strip().lower()
                            for k in ("trade_id", "trade_date", "isin", "trade_type", "quantity", "price"))   # trade_id alone is not unique
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
    rows.sort(key=lambda r: (r.get("trade_date", ""), (r.get("trade_type") or "").strip().lower() == "sell"))
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", type=date.fromisoformat, required=True, help="first day to fetch, YYYY-MM-DD (your first Coin purchase or earlier)")
    ap.add_argument("--until", type=date.fromisoformat, default=today_ist(), help="last day, default today")
    ap.add_argument("--segment", default="MF", choices=["MF", "EQ"], help="Console segment: MF (mutual funds, default) or EQ (stocks)")
    ap.add_argument("--stdin", action="store_true", help="read the cookie header, then the x-csrftoken, one per line from stdin")
    ap.add_argument("--out", type=Path, default=Path("tradebooks"), help="directory for tradebook-FY*.csv (default ./tradebooks)")
    ap.add_argument("--merge", action="store_true", help="also write tradebook-all.csv, every year merged and deduplicated on trade id, date, ISIN, type, quantity and price")
    ap.add_argument("--pause", type=float, default=8.0, help="seconds between years (default 8; Console rate-limits eager clients)")
    ap.add_argument("--poll", type=float, default=5.0, help="seconds between polls while Console builds a report")
    ap.add_argument("--wait", type=float, default=240.0, help="how long to keep polling one report before giving up")
    ap.add_argument("--force", action="store_true", help="fetch a year even when its file already exists")
    ap.add_argument("--dry-run", action="store_true", help="print the windows and stop")
    a = ap.parse_args()
    prefix = "tradebook-eq-" if a.segment == "EQ" else "tradebook-"
    windows = list(financial_years(a.since, a.until))
    if not windows:
        raise SystemExit("nothing to fetch: --since is after --until")
    if a.dry_run:
        for label, frm, to in windows:
            print(f"{label}: {frm} to {to}")
        return
    cookie, csrf = config.CONSOLE_COOKIE, config.CONSOLE_CSRF
    if a.stdin:   # cookie, then token
        lines = [*sys.stdin.read().splitlines(), "", ""]
        cookie, csrf = lines[0], lines[1]
    elif (not cookie or not csrf) and sys.stdin.isatty():
        import getpass
        cookie = cookie or getpass.getpass("Console cookie header (hidden): ")
        csrf = csrf or getpass.getpass("x-csrftoken (hidden): ")
    if not cookie or not csrf:
        raise SystemExit("both the cookie and the x-csrftoken are needed: type them at the prompts, set CONSOLE_COOKIE "
                         "and CONSOLE_CSRF, or pipe them with --stdin")
    headers = {"cookie": cookie.strip(), "x-csrftoken": csrf.strip(), "referer": REFERER, "user-agent": USER_AGENT,
               "accept": "text/csv,application/json;q=0.9,*/*;q=0.8", "accept-language": "en-GB,en;q=0.9",
               "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty",
               "x-requested-with": "XMLHttpRequest"}
    a.out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with httpx.Client(headers=headers, timeout=90, follow_redirects=False) as client:
        for i, (label, frm, to) in enumerate(windows):
            path = a.out / f"{prefix}{label}.csv"
            closed_year = to < a.until or to < today_ist() - timedelta(days=3)
            if path.exists() and closed_year and not a.force:
                existing = path.read_text(encoding="utf-8-sig")
                if within(existing, frm, to, a.segment):
                    print(f"{label}: already fetched -> {path} (use --force to refetch)")
                    seen_reports.add(hashlib.sha256(existing.encode()).hexdigest())
                    written.append(path)
                    continue
                ds = dates_in(existing)
                print(f"{label}: {path} holds {f'trades dated {ds[0]}..{ds[-1]}' if ds else 'no readable trades'}, not this year; refetching")
            if i:
                time.sleep(a.pause)
            text = fetch_window(client, frm, to, a.segment, poll=a.poll, wait=a.wait)
            n = rows_in(text)
            if not n:
                print(f"{label}: no trades between {frm} and {to}")
                continue
            path.write_text(text.lstrip("\ufeff"), encoding="utf-8")
            written.append(path)
            print(f"{label}: {n} trades ({frm} to {to}) -> {path}")
    if a.merge and written:
        out = a.out / ("tradebook-eq-all.csv" if a.segment == "EQ" else "tradebook-all.csv")
        print(f"merged: {merge(written, out)} unique trades -> {out}")
    if not written:
        print("no tradebook rows in the whole range", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
