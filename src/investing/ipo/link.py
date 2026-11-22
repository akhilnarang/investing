"""Link a prospectus to the company it is about.

An RHP is filed before the company has an ISIN, so the link is made by name after it lists.
NSE truncates security names, so the listed name is matched as a prefix, and only a unique match
is accepted: a wrong ISIN would attach the prospectus to another stock's prices and holders.
"""
import re
import sqlite3
import unicodedata

# Legal-form words, dropped from both sides before comparing.
NOISE = frozenset({"limited", "ltd", "private", "pvt", "public", "company", "co", "the", "india"})


def normalize(name: str) -> str:
    """'Milky Mist Dairy Food Limited' and 'MILKY MIST DAIRY FOOD L' both start 'milky mist dairy food'."""
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace("&", " and ")
    words = [w for w in re.split(r"[^a-z0-9]+", text) if w and w not in NOISE]
    # A trailing single letter is NSE truncating a word ('...FOOD L' from 'LIMITED').
    if len(words) > 1 and len(words[-1]) == 1:
        words.pop()
    return " ".join(words)


# NSE truncates security names to this many characters. Many listed names sit exactly on it.
NSE_NAME_LIMIT = 25
# Shorter prefixes are not evidence: NSE lists a company named 'MIL', a prefix of hundreds of names.
MIN_MATCH = 8


def _prefix_match(target: str, listed: str, truncated: bool) -> bool:
    """Whether prospectus name `target` and listed name `listed` are one company.

    Only a listed name at the truncation limit may stop mid-word, so 'sun pharma' does not match
    'sun pharmaceutical advanced research'."""
    if not listed or not target:
        return False
    if listed == target:
        return True
    if truncated:
        return len(listed) >= MIN_MATCH and target.startswith(listed)
    # Not truncated, so only a whole-word extension can match.
    return len(listed) >= MIN_MATCH and target.startswith(listed + " ")


def resolve(db: sqlite3.Connection, company_name: str) -> str | None:
    """Find the ISIN for an extracted issuer name, or None if absent or ambiguous."""
    target = normalize(company_name)
    if len(target) < MIN_MATCH:
        return None
    try:
        rows = db.execute("SELECT isin, name FROM company WHERE name IS NOT NULL").fetchall()
    except sqlite3.OperationalError:
        return None  # an rhp-only database has no company table yet
    hits = {
        row["isin"]
        for row in rows
        if _prefix_match(target, normalize(row["name"]), len(row["name"].strip()) >= NSE_NAME_LIMIT)
    }
    return hits.pop() if len(hits) == 1 else None


def link_analyses(db: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Fill in every analysis whose company has since listed. Returns what it linked.

    Only rows with a null isin are considered, so an existing link is never overwritten."""
    pending = db.execute(
        "SELECT analysis_id, company_name FROM analysis_jobs WHERE isin IS NULL AND company_name IS NOT NULL"
    ).fetchall()
    linked = []
    for row in pending:
        isin = resolve(db, row["company_name"])
        if isin is not None:
            db.execute("UPDATE analysis_jobs SET isin = ? WHERE analysis_id = ?", (isin, row["analysis_id"]))
            linked.append((row["analysis_id"], row["company_name"], isin))
    return linked


def prospectus_for(db: sqlite3.Connection, isin: str) -> list[sqlite3.Row]:
    """Every analyzed prospectus for one listed company, newest first."""
    return db.execute(
        "SELECT analysis_jobs.*, report_cache.report_markdown FROM analysis_jobs "
        "LEFT JOIN report_cache ON report_cache.cache_key = analysis_jobs.analysis_id "
        "WHERE analysis_jobs.isin = ? ORDER BY analysis_jobs.created_at DESC, analysis_jobs.analysis_id",
        (isin,),
    ).fetchall()
