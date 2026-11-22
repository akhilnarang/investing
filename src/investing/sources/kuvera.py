"""Disclosed portfolio holdings from Kuvera's public API, keyed by AMFI scheme code and stock ISIN.

Kuvera uses its own codes (PP001ZG-GR). Each AMFI code is matched once by ISIN and the match is
stored in kuvera_code, since matching costs requests against a rate-limited API.

Kuvera rate-limits per public address, so requests go through `egress.rotation`, paced per exit.
"""
import json
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..core import egress
from ..db import day, writing
from . import UA, cache_dir

LIST_URL = "https://api.kuvera.in/mf/api/v4/fund_schemes/list.json"
SCHEME_URL = "https://api.kuvera.in/mf/api/v5/fund_schemes/{code}.json"
HOLDINGS_URL = "https://api.kuvera.in/mf/api/v5/fund_portfolio_holdings/{code}.json"
# Funds Kuvera has no match for (regular plans, IDCW variants, wound-up schemes), retried after
# RETRY_AFTER in case Kuvera adds them.
UNRESOLVED = cache_dir("kuvera") / "unresolved.json"
RETRY_AFTER = timedelta(days=28)

NOISE = re.compile(r"\b(direct|regular|plan|growth|option|fund|scheme|the|of|an?)\b")
CAPS = re.compile(r"\b(flexi|small|mid|large|multi|micro)\s+cap\b")   # Kuvera "Flexicap", Groww "Flexi Cap"

POOL: egress.Pool | None = None
_FALLBACK: egress.Pool | None = None


def _pool() -> egress.Pool:
    """The rotation `fetch` opened, or one direct exit built once and kept, so pacing carries over."""
    global _FALLBACK
    pool = POOL                      # read once: fetch clears it when its rotation closes
    if pool is not None:
        return pool
    if _FALLBACK is None:
        _FALLBACK = egress.Pool([egress.local_exit()], 0.35, UA)
    return _FALLBACK


def get_json(url: str, tries: int = 5):
    return _pool().get_json(url, tries=tries)


def write_json(path: Path, payload: Any, *, indent: int | None = None, compact: bool = True) -> None:
    """Write a JSON document atomically: temp file, fsync, rename, fsync the directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=indent, separators=(",", ":") if compact and indent is None else None,
                      ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def tokens(name: str) -> set[str]:
    name = CAPS.sub(lambda m: m.group(1) + "cap", (name or "").lower())
    return {t for t in re.sub(r"[^a-z0-9]+", " ", NOISE.sub(" ", name)).split() if len(t) > 1}


MIN_SCHEMES = 900     # a real listing; anything less is a truncated or reshaped response


def _walk(node, out: list[dict]) -> None:
    """Collect scheme records from a flat list of {code, name} or the nested
    category -> sub-category -> AMC -> [{"c": code, "n": name}] tree."""
    if isinstance(node, dict):
        code = node.get("code") or node.get("c")
        name = node.get("name") or node.get("n")
        if code and name and isinstance(code, str):
            out.append({"code": code, "name": name,
                        "category": node.get("fund_category") or node.get("category")})
            return
        for v in node.values():
            _walk(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out)


def kuvera_list() -> list[dict]:
    out: list[dict] = []
    _walk(get_json(LIST_URL), out)
    seen: dict[str, dict] = {}
    for r in out:
        seen.setdefault(r["code"], r)
    if len(seen) < MIN_SCHEMES:
        raise RuntimeError(f"Kuvera listed only {len(seen)} schemes; refusing to map against it")
    return list(seen.values())


def scheme_isin(code: str) -> str | None:
    payload = get_json(SCHEME_URL.format(code=code))
    row = payload[0] if isinstance(payload, list) and payload else payload
    return (row or {}).get("ISIN") or None


def resolve(name: str, isins: set[str], entries: list[dict], shortlist: int = 6) -> dict | None:
    """The Kuvera code whose ISIN equals one of the scheme's, trying the closest names first.

    None only when every candidate answered and none matched. A failed lookup raises, so an outage
    is not recorded as a miss."""
    if not isins:
        return None
    want = tokens(name)
    brand = (re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split() or [""])[0]

    def score(e: dict) -> tuple:
        have = tokens(e["name"])
        return (brand in have, len(want & have) / max(len(want | have), 1))

    unasked: Exception | None = None
    for e in sorted(entries, key=score, reverse=True)[:shortlist]:
        if not score(e)[0] and not (want & tokens(e["name"])):
            break
        try:
            isin = scheme_isin(e["code"])
        except Exception as exc:  # a candidate that cannot be asked is retried next run
            unasked = exc
            continue
        if isin in isins:
            return {"kuvera_code": e["code"], "isin": isin, "kuvera_name": e["name"]}
    if unasked is not None:
        raise RuntimeError(f"kuvera: could not check every candidate for {name!r}") from unasked
    return None


def store(db, scheme_code: int, kuvera_code: str, rows: list[dict]) -> tuple[int, str | None]:
    """One fund's disclosed portfolio. Kuvera serves only the latest, so history accrues per date."""
    dated = [r for r in rows if r.get("portfolio_date")]
    if not dated:
        return 0, None
    portfolio_date = max(r["portfolio_date"] for r in dated)
    d = day(datetime.fromisoformat(portfolio_date).date())
    stored = []
    for r in rows:
        if (r.get("portfolio_date") or portfolio_date) != portfolio_date:
            continue
        name = r.get("security_name") or r.get("company_name")
        pct = r.get("percentage_to_aum")
        # Kuvera keeps listing positions a fund has exited, at zero weight and zero value.
        if not name or pct is None or pct <= 0:
            continue
        isin = r.get("holding_isin") or None
        company = r.get("company_name")
        stored.append((scheme_code, d, name, isin, company if company != name else None,
                       pct, r.get("value_in_mn"), None, r.get("sector_name"),
                       r.get("security_asset_class") or None, None,
                       isin or "", ""))
    if not stored:
        return 0, portfolio_date
    db.executemany(
        "INSERT OR REPLACE INTO holding (scheme_code, d, name, isin, company, weight, value, "
        "quantity, sector, asset_class, side, isin_key, side_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", stored)
    return len(stored), portfolio_date


def _unresolved() -> dict[str, str]:
    try:
        return json.loads(UNRESOLVED.read_text())
    except (OSError, ValueError):
        return {}


def _due(misses: dict[str, str], code: int, now: datetime) -> bool:
    # Spread retries over a further four weeks by code so they do not all fall due in one run.
    seen = misses.get(str(code))
    return seen is None or now - datetime.fromisoformat(seen) >= RETRY_AFTER + timedelta(days=code % 28)


def fetch(db, only: list[int] | None = None, min_gap: float = 0.35) -> str:
    """Refresh every mapped fund's portfolio, mapping any that are not mapped yet."""
    global POOL
    funds = [dict(r) for r in db.execute(
        "SELECT f.scheme_code, f.name, f.isin_growth, k.kuvera_code "
        "FROM fund f LEFT JOIN kuvera_code k ON k.scheme_code = f.scheme_code "
        "WHERE f.isin_growth IS NOT NULL" +
        (f" AND f.scheme_code IN ({','.join('?' * len(only))})" if only else ""),
        [int(c) for c in only] if only else [])]
    if not funds:
        return "kuvera: no funds with an ISIN to map"

    mapped = resolved = rows = failed = skipped = 0
    now = datetime.now(UTC)
    misses = _unresolved()
    with egress.rotation(min_gap=min_gap, user_agent=UA) as pool:
        POOL = pool
        try:
            entries: list[dict] | None = None
            for f in funds:
                code = int(f["scheme_code"])
                kcode = f["kuvera_code"]
                if not kcode:
                    if not only and not _due(misses, code, now):
                        skipped += 1
                        continue
                    if entries is None:
                        entries = kuvera_list()
                    try:
                        got = resolve(f["name"], {f["isin_growth"]}, entries)
                    except RuntimeError:
                        failed += 1     # not remembered: asked again next run
                        continue
                    if not got:
                        misses[str(code)] = now.isoformat()
                        failed += 1
                        continue
                    misses.pop(str(code), None)
                    kcode = got["kuvera_code"]
                    with writing(db):
                        db.execute("INSERT OR REPLACE INTO kuvera_code VALUES (?,?,?,?,?)",
                                   (code, kcode, got["isin"], got["kuvera_name"],
                                    datetime.now(UTC).isoformat()))
                    resolved += 1
                try:
                    payload = get_json(HOLDINGS_URL.format(code=kcode))
                except Exception:  # one fund failing does not stop the run
                    failed += 1
                    continue
                got_rows = payload.get(kcode) if isinstance(payload, dict) else payload
                with writing(db):
                    n, _ = store(db, code, kcode, got_rows or [])
                if n:
                    mapped += 1
                    rows += n
        finally:
            POOL = None
            # Concurrent runs are prevented by the refresh lock in scripts/refresh.py.
            write_json(UNRESOLVED, misses)
    return (f"kuvera: {mapped:,} portfolios, {rows:,} holdings"
            + (f", {resolved} newly mapped" if resolved else "")
            + (f", {failed} failed" if failed else "")
            + (f", {skipped} unmatched skipped until due" if skipped else ""))
