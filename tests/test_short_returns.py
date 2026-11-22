from datetime import date, timedelta

from investing.db import connect, day
from investing.web import queries as q


def test_short_returns_measure_to_one_day_and_rank(tmp_path):
    db = connect(tmp_path / "t.sqlite3")
    end = date(2026, 10, 23)
    # 200 daily points to clear nav.MIN_POINTS. Fund 1 gains 1% a day for the last week, fund 2 is
    # flat, fund 3 is a day behind and is not ranked on a different week.
    for code, stop, gain in ((1, end, 1.01), (2, end, 1.0), (3, end - timedelta(days=1), 1.0)):
        db.execute("INSERT INTO fund (scheme_code, name, plan, meta) VALUES (?,?,?,'{}')",
                   (code, f"F{code}", "direct"))
        nav = 100.0
        for i in range(200, -1, -1):
            d = end - timedelta(days=i)
            if d > stop:
                continue
            if i < 7:
                nav *= gain
            db.execute("INSERT INTO nav (scheme_code, d, nav) VALUES (?,?,?)", (code, day(d), nav))
    db.commit()
    out = q.short_returns(db, [1, 2, 3])
    assert out["as_of"] == end.isoformat()
    f = out["funds"]
    assert round(f["1"]["1w"], 2) == round((1.01 ** 7 - 1) * 100, 2) and f["1"]["1w_rank"] == 1
    assert f["2"]["1w"] == 0 and f["2"]["1w_rank"] == 2
    assert f["3"]["1w"] is None and "1w_rank" not in f["3"]
    assert out["counts"]["1w"] == 2


def test_one_fund_ahead_does_not_blank_the_rest(tmp_path):
    db = connect(tmp_path / "t.sqlite3")
    end = date(2026, 10, 23)
    for code, stop in ((1, end), (2, end), (3, end + timedelta(days=1))):
        db.execute("INSERT INTO fund (scheme_code, name, plan, meta) VALUES (?,?,?,'{}')",
                   (code, f"F{code}", "direct"))
        for i in range(200, -2, -1):
            d = end - timedelta(days=i)
            if d <= stop:
                db.execute("INSERT INTO nav (scheme_code, d, nav) VALUES (?,?,?)", (code, day(d), 100.0))
    db.commit()
    out = q.short_returns(db, [1, 2, 3])
    assert out["as_of"] == end.isoformat() and out["counts"]["1w"] == 3
