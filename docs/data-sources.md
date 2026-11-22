# Data sources and terms

The data comes from these places:

- AMFI: NAVs, scheme lists and TER.
- NSE: bhavcopy archives, index total return values, and the live site APIs behind SLB and option chains.
- niftyindices.com: index constituents.
- Groww and Kuvera: scheme facts and universe.
- Yahoo Finance: US indexes, metals futures, splits and dividends.
- ECB reference rates and World Bank CPI.
- IBJA: gold and silver rates.
- AMC websites: portfolio disclosures.
- The IPO registrars KFintech, MUFG Intime, Bigshare, Purva and Maashitla: allotment status.
- Zerodha Console: your own tradebook, through `coin-tradebook`.

Several of these are undocumented endpoints that can change or disappear without notice. What the code does:

- The default User-Agent is a desktop browser string, because NSE and KFintech refuse other agents.
  `INVESTING_USER_AGENT` changes it.
- `core/egress.py` can rotate requests over the machine's own addresses to stay under per-address limits.
  Two registrar adapters can send requests through an HTTP proxy.
- You run the fetchers at your own risk and must check each site's terms of use.
- The repository ships no fetched data. The database is not redistributed.
- A public deployment re-serves live NSE SLB and option data to visitors, rate limited per address.
- `coin-tradebook` automates your own logged-in Console session against an endpoint Zerodha does
  not document.
