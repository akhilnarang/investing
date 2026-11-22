# Repository guidance

## Testing

- Strongly avoid creating new tests. Extend an existing focused test when it can cover the changed
  behavior; add a case only for an otherwise uncovered contract.
- Assert real values, not shapes. A series read one index high still has the right row count; only
  the values show that every date shifted.
- Tests run on the seeded database or on the real one. A test either asserts an invariant that holds
  on both, or carries `@pytest.mark.real_db` and skips on the seed.
- No network in tests. Mock at the fetcher boundary or parse a recorded payload, and check that
  meaningful data crosses the boundary. A canned fake answer alone proves nothing.
- Cover the hot path and at most one failure case per contract. A parameterized variation counts as
  a case; keep it only for distinct behavior.
- Do not test private structure or facts the typechecker already checks.
- Keep web tests to user-visible behavior: a page renders, a link off it resolves. Do not assert
  exact markup or CSS.
- Never put real personal data (a PAN, a name, a client ID, a tradebook) in a test.
- When pruning tests, keep distinct behavior; do not chase test counts or coverage scores.

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and the rules below in short.

## What is frozen

`src/investing/db.py` owns the schema, including `RHP_SCHEMA`, the prospectus tables. A column, table
or index is added there and nowhere else.
`connect()` refuses to open a database whose tables predate the current schema, so a stale file
fails at open rather than quietly mid-write.

`src/investing/analysis/nav.py` is accumulated knowledge about which funds carry bad data: the Axis zero
on 2013-04-07, UTI Liquid's 1/10 redenomination, quant's bonus issues. It is not a parser. A wrong
repair silently changes every return that crosses it.

## Data direction

Fetchers in `src/investing/sources/` pull from upstream, cache the raw response, and parse into the
database. One direction. The cache is what makes a parse bug free to fix: re-run against it rather
than re-fetching, which matters most for AMFI where a full pull is the better part of an hour.

`scripts/refresh.py` runs them in dependency order. `amfi` creates the fund rows `universe`, `ter`,
`kuvera` and `amc` key off; `fx` writes the usd-inr series `us_indexes` converts with.

The documents derived from those tables are built by functions in `investing.analysis`, not scripts:
`arbitrage.write`, `blend.write`, `longterm.write_all`, `tracking.write` and
`attribution.write_all` each take a `sqlite3.Connection` and return a one-line summary.
`scripts/refresh.py` calls them in-process after the fetches, through its `DERIVED` map. `scripts/`
holds only `refresh.py` and `deploy.sh`; new work goes in the package as a function.

## Staleness

Data being a day behind is not staleness. Yesterday's NAV is a real NAV, and AMFI publishes late in
the evening anyway, so a page showing yesterday is correct rather than broken.

What is staleness is data produced by code that no longer exists. If you change a fetcher, a parser,
a schema or a document builder, the rows and documents it wrote before the change are now the output
of logic nobody can read any more. Re-run it. That is not optional politeness, it is the difference
between a number you can explain and one you cannot.

Two cases to keep in mind:

**A derived document is only as good as the data under it.** If you generate an analysis document,
regenerate it after the next fetch of its inputs, or say plainly in your report that it needs
regenerating. `scripts/refresh.py` does this after each source (see docs/refresh.md).

**A schema change orphans existing rows.** `connect()` refuses to open a database whose tables
predate the schema, which catches the structural case. It cannot catch a parser that now reads a
field differently: those rows still fit the schema and are still wrong. Re-import.

## Dates

INTEGER days since 1970-01-01 in the database, `YYYY-MM-DD` at every boundary. Use `day()` and
`undate()` from `db.py`. Do not do the arithmetic by hand. The current Indian market date comes from
`today_ist()` in `investing/core/clock.py`.

## Imports

`investing` is an installed package, so `import investing.web.queries` works from anywhere under
`uv run`. No `sys.path.insert` in scripts, tests or anywhere else: it hides a broken environment
rather than fixing one.

## Templates

Jinja autoescapes `.html`, so `{{ }}` is safe in HTML text and attributes and `|e` is redundant
there. It is not safe anywhere else. A value interpolated into a `<script>` block, a `style`
attribute or a URL is escaped for HTML and still wrong for those contexts: put data in a `data-`
attribute and read it from JavaScript rather than templating into script. Never reach for `|safe` or
`Markup()` on anything that came from upstream or from a query string.

`{{ q }}` where `q` is None renders the string "None". Use `{{ q or '' }}`.

## The site

One stylesheet, `src/investing/web/static/site.css`, no library. Table sorting is one shared script,
`src/investing/web/static/sort.js`: a table opts in with `data-sortable` and its headers carry `data-key`.
The static URL carries a hash of every file under `static/`, so a deploy is not defeated by a CDN
cache. The hash is computed at import, so restarting the service is what publishes a CSS or script
change.

Two things about it have each caused more than one bug:

**Bare element selectors match more than you think.** The analyzer's templates bring `<header>`,
`<nav>` and `<footer>` elements inside the page, and a rule written for the site chrome will style
them too. `header` is scoped to `body > header` for this reason. Write a class.

Specificity is not the protection it looks like. A class beats an element selector, so a rule like
`.report-header` does win, but only for the properties it names: a bare `header` rule still applies
everything else it declares to that element. The failure is quiet in both directions, because a
bare rule can also lose where you wanted it to win.

**A template block only exists if a base template renders it.** `site_base.html` has `title`,
`head`, `body` and `tail`, and every page extends it directly. Anything else is silently dropped
and the page looks fine while doing nothing.

Check a visual change in a browser at a desktop and a mobile width, not only by reading computed
styles.

## Style

Comments explain why, not what, and earn their place. Write them in the present tense and leave out
history. Match the surrounding code. Plain technical English, short sentences. No em
dashes and no en dashes anywhere, including copy on the site.

- HTTP errors come from `investing/web/errors.py`, not `HTTPException` in a route.
- Repeated string literals are StrEnums in `investing/core/enums.py`.
- Indian market dates come from `investing/core/clock.py`.
- `logging.getLogger(__name__)` in each module. No `print()` in library code.
- Route paths are full string literals.
- Validate input at the boundary with Pydantic or FastAPI parameters.
- An async handler never blocks: use `asyncio.to_thread`.
- Every query has an explicit `ORDER BY` with a tie-breaker.
- Fix lint instead of adding `# noqa`. Lint is `uv run ruff check`.
- Use `is not None` for presence checks where 0 or an empty string is a valid value.
- Personal data (PANs, names, tradebooks) is never logged, cached or stored server-side.
