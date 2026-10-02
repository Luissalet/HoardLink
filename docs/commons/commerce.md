# Commerce: money, dates, identifiers, tracking, merchants, calendar files, business days

What every shop-, bank- and paperwork-minded app copied from the others: reading an amount, finding a date, checking a
DNI or an IBAN, recognising a parcel number or a shop, writing an `.ics` file, counting working days. Standard library
only (see `docs/COMMONS.md`). Python modules live in `hoard_link/`, the Node twins in `js/hoard-commons/` (ESM, no npm
dependencies; same names in camelCase; `merchants.js` and `ics.js` use `node:` modules, so they are server-side).
Tests: `tests/commons/test_<module>.py`; both languages run the same vectors in `tests/vectors/`
(`money`, `dates`, `idcheck`, `tracking`, `merchants_cases`, `ics`). `bizdays` is Python only.

| Module | Replaces | Node twin |
|---|---|---|
| `money` | Ledger `shared/money.js` + `server/mail-parse.js findMoney`, Tantalus `extract/phrases.py` + `mail/parse.py` price code, Phileas `mail/parse.py find_price` / `_amount`, Mercator `mercator.py parse_amount`, hub `purchases.py _to_amount / fmt_amount`, Phileas `travel/expenses.py settle` | `money.js` |
| `dates` | Tantalus `parse_date`, Kafka `extract/dates.py`, Phileas `mail/parse.py` month tables and `find_eta`, Mercator `parse_date`, People `server/dates.js` | `dates.js` |
| `idcheck` | Kafka `privacy.py`, Pygmalion `datasets/pii.py`, product-id regexes in Tantalus | `idcheck.js` |
| `tracking` | Phileas `numbers.py`, Tantalus `mail/parse.py clean_url` | `tracking.js` |
| `merchants` | Ledger `mail-merchants.js` / `mail-match.js` / `recurring.js merchantKey`, hub `purchases.py` (`order_key`, `merchant_*`), Phileas `ORDER_PATTERNS`, Ledger `REF_PATTERNS`, Kafka `issuers.py` names, Tantalus `mail/stores.py` | `merchants.js` + `merchants.json` |
| `ics` | Faustus `routes/calendar_routes.py` export, Phileas `travel/ics.py`, People `server/calendar.js` | `ics.js` |
| `bizdays` | Kafka `bizdays.py`, Phileas `bizdays.py` | - |

## `hoard_link.money`

```python
parse_amount(text, *, decimal=None, currency_hint=None, lang="es") -> Decimal | None
parse_cents(text, *, decimal=None, currency_hint=None, lang="es") -> int | None
to_cents(value) -> int | None            from_cents(cents) -> Decimal
detect_decimal(samples) -> "," | "." | None                   # a CSV column
find_prices(text, *, labelled_only=False, lang="es") -> list[PriceHit]   # PriceHit(amount, currency, start, end, label)
format_money(value, currency="EUR", lang="es", *, trim_zero_cents=False, grouping=True, nbsp=False) -> str
format_cents(cents, currency="EUR", lang="es", **kw) -> str       currency_of(marker) -> "EUR"
split_shares(total_cents, weights) -> list | dict                # largest remainder, parts add up
settle(balances: {name: cents}) -> [{"from", "to", "cents"}]     # fewest transfers (exact up to EXACT_LIMIT = 16 people)
```

Decisions (they differed per app): the right-most separator is the decimal mark when both appear (`1.234,50`,
`1,234.50`); a repeated separator is thousands (`1.234.567`); a lone `.` or `,` followed by exactly three digits after one
to three digits is **ambiguous** and read as thousands (`"1.234"` -> 1234, `"2,099"` -> 2099, with or without a currency),
except for a `.` in a dot-decimal context (`lang="en"`, or USD/GBP/... from `currency_hint` or the text: `"$1.234"` is
1.234). `"12,5"`, `"59.99"`, `"0,123"` are decimal. Signs `-12,50`, `−12,50`, `12,50-`, `(12,50)` are negative. Spanish output
is `1.234,56 €`, English `€1,234.56`, French uses U+202F. `parseAmount` in JS returns a Number rounded to cents; use
`parseCents` for exact work.

## `hoard_link.dates`

```python
parse_date(text, *, today=None, lang="es", dayfirst=True, prefer="nearest") -> date | None
find_dates(text, *, today=None, lang="es", dayfirst=True, loose=False) -> list[DateHit]   # DateHit(date, start, end, role, text)
parse_due(text, *, today=None, lang="es", vague=False) -> date | None     # "mañana", "el viernes", "en dos semanas", "fin de mes"
add_days / add_months (clamps) / days_between / next_weekday / resolve_year / iso_day / parse_iso / month_number / weekday_number
```

Months and weekdays in es, en, fr, pt, it, de; roles `due, expires, issued, delivery, departure, return, purchase, renewal`
come from the label before the date. A date without a year gets the year that suits its role (future for deadlines, past for
issue dates). Always pass `today` in tests. In JS dates travel as `YYYY-MM-DD` strings.

## `hoard_link.idcheck`

```python
luhn_ok, iban_ok, dni_ok, nie_ok, cif_ok, nif_ok, ean_ok, isbn_ok (value) -> bool        asin(text) -> str | None
identifiers(text, url=None) -> {"ean", "asin", "isbn", "sku"}
scan_pii(text, kinds=None) -> list[Hit]   # Hit(kind, start, end, value); kinds: DNI NIE CIF IBAN CARD EMAIL PHONE_ES
mask_text(text, kinds=None, token="<{kind}>") -> str       mask_obj(obj, **kw)
```

`scan_pii` only reports values whose checksum is right, so a reference that merely looks like an ID is left alone. Phones
are reported with a `+34`/`0034` prefix, after a label, or written in groups; nine bare digits are not.

## `hoard_link.tracking`

```python
find(text, links=None) -> list[Tracking]        # Tracking(number, carrier, confidence, evidence, url, notes); .to_dict()
from_url(url) / unwrap(url) / classify(number) -> (carrier, confidence) / plausible(number) / carriers_mentioned(text)
ups_valid, s10_valid, normalize, tracking_url(carrier, number), carrier_name(carrier), CARRIERS, CARRIER_WORDS
clean_url(url) -> str                            # strips utm_*, gclid... and unwraps redirect links (defers to hoard_link.web.urls)
```

The numbers, confidences and carrier table are Phileas's `numbers.py`. In JS `cleanUrl` defers to `./web.js` when present and
keeps a local copy otherwise (the vectors check both agree).

## `hoard_link.merchants`

```python
lookup(name_or_domain_or_sender) -> dict | None       # id, display, domains, aliases, patterns, category, flags, tracking_id, key
merchant_id(x) -> str | None        merchant_key(name) -> str        plain_key(name, *, strip_legal=True) -> str
merchant_tokens(text) -> list[str]  merchant_similar(a, b) -> bool   category_of(x) -> str | None   category_hints(category) -> [str]
carrier_of(x) -> tracking carrier id | None    is_subscription / is_carrier / is_gateway / is_bank (x)    is_noise_sender(sender)
find_merchants(text) -> [{"id", "start", "end"}]
order_key(ref) -> str  (upper-case alphanumerics, "" under 4 chars)    find_order_refs(text, *, limit=8) -> [str]    find_order_ref(text) -> str
```

The data is `hoard_link/_data/merchants.json` (188 merchants: streaming, cloud, telecom, utilities, insurance, transport, food, shops,
banks, carriers) and a byte-identical copy next to the JS (a test fails if they differ). **The list order is a priority**
(AWS before Amazon, Correos Express before Correos, Amazon Prime before Amazon). Resolution order: exact sender address, domain
(longest wins, subdomains count), exact name or alias after dropping the legal suffix (`S.L.`, `SAU`, `Ltd`, `GmbH`), then patterns.
Add a shop by editing the JSON (and copying it to the JS folder); patterns are folded lower-case regexes that must compile in both languages.

`merchant_similar`: two known merchants are the same only if their ids are; otherwise all words of one are in the other, or half of
all words are shared, or a 4+ letter word is glued into another (`NETFLIXCOM` / `Netflix Billing`). So `PcComponentes` = `PC COMPONENTES SL`,
`Amazon` = `AMZN Mktp ES`, `NETFLIXCOM` = `Netflix`, `El Corte Inglés` = `ECI`, but `Amazon Prime` is not `Amazon Web Services` and
`Bar Manolo` is not `Bar Pepe`. A payment processor in front (`PAYPAL *SPOTIFY`) resolves to the shop. Order references: Amazon `3-7-7`, Google
Store `GS.`, Google Play, then labels (`pedido n.º`, `order #`, `Bestellung`, `commande`, `factura`, `ref.`); dates, UPS numbers and anything with
under three digits are skipped.

## `hoard_link.ics`

```python
build_ics(events, *, name=None, prodid="-//Hoard//Family//EN", tz=None, now=None) -> str      # CRLF, folded at 75 octets
parse_ics(text) -> [{"uid", "summary", "description", "location", "start", "end", "all_day", "end_exclusive", "tzid", "rrule", "url", "status", "categories", "alarms"}]
ics_escape(text), ics_unescape(text), fold_line(line, limit=75)
```

Event keys: `uid|id`, `title|summary`, `start`, `end`, `all_day`, `end_exclusive`, `tz|tzid`, `description|detail`, `location`, `url`,
`categories`, `rrule` (a leading `RRULE:` is dropped), `status`, `priority`, `transp`, `alarms` (minutes before, `{"minutes": n}` or
`{"at": ISO}`). For an all-day event `end` is the **last day** (`end_exclusive: true` if you already have the ICS DTEND). Aware times are
written in UTC, local times get `TZID=` (no VTIMEZONE block) or are floating. UIDs without `@` get `@hoard`; a missing UID is a hash of
title, start and place. Pass `now` for reproducible DTSTAMPs. `parse_ics` output feeds straight back into `build_ics`.

## `hoard_link.bizdays` (Python only)

```python
easter(year) -> date          holidays(year, region="ES", *, move_sunday=None) -> {date: name}
Calendar(region="ES", extra=(), *, weekend=(5, 6), move_sunday=None)
  .is_business_day(d)  .add_business_days(d, n)  .next_business_day(d, *, include_self=False)  .previous_business_day(d, ...)
  .business_days_between(a, b)  .holiday_name(d)  .is_delivery_day(d, carrier)  .next_delivery_day(d, carrier, ...)
CARRIER_WEEKDAYS, delivery_weekdays(carrier)
```

Regions: `ES` (national) and `ES-MD`, `ES-CT`, `ES-AN`, `ES-VC`, `ES-GA`, `ES-PV`. Dates in as `date` or ISO text, out the same way. `business_days_between` counts
`[a, b)`. **Best-effort tables:** autonomous communities change their calendar every year and local holidays are not included; for anything legal
pass the official dates in `extra`. A fixed holiday on a Sunday moves to Monday only for 1 Jan, 6 Jan, 6 Dec, 8 Dec and 25 Dec (Catalonia moves none);
this is data (`MOVE_SUNDAY`, per region `move_sunday`) and can be overridden per call.

## Migration notes per app

* **Tantalus** — `extract/phrases.py` (`find_prices`, `parse_price`, `parse_date`) -> `money.find_prices`, `dates.parse_date`; `mail/parse.py` (`fold`, `parse_amount`, `prices`, `clean_url`)
  -> `text.fold`, `money.parse_amount`, `money.find_prices`, `tracking.clean_url`; `notify/labels.py format_price` -> `money.format_money`; `mail/stores.py store_for` ->
  `merchants.lookup` (keep the user's own store list on top). Prices are Decimals now: store cents (`parse_cents`).
* **Kafka** — `bizdays.py` -> `bizdays` (`holidays` returned a `frozenset`; now a `{date: name}` dict, same keys; `region=""` is `"ES"`); `privacy.py` (`_luhn`, `_iban_ok`, `mask_text`, `mask_obj`)
  -> `idcheck`; `extract/dates.py` -> `dates.find_dates` (roles replace its own labels); `extract/issuers.py` company names -> `merchants.lookup`.
* **Phileas** — `numbers.py` -> `tracking`; `bizdays.py` -> `bizdays` (`DELIVERY_WEEKDAYS` -> `CARRIER_WEEKDAYS`, `Calendar` keeps the same methods plus `next_delivery_day`); `ORDER_PATTERNS`, `find_price`,
  month tables in `mail/parse.py` -> `merchants.find_order_refs`, `money.find_prices`, `dates`; `travel/ics.py` -> `ics.build_ics` (lodging: `end_exclusive: True`; the check-in alarm is
  `{"at": ...}`); `travel/expenses.py settle` -> `money.settle` (same exact grouping, ids sorted).
* **Ledger** — `shared/money.js` + `server/money.js` -> `money.js` (`parseAmount`, `detectDecimal`, `formatCents`; note the `"1.234"` rule above); `server/mail-parse.js` `findMoney`/`REF_PATTERNS`/`findOrderRef` ->
  `findPrices` / `findOrderRefs` (first element = old result); `mail-merchants.js` table, `mail-match.js` `merchantTokens`/`merchantMatches`, `recurring.js merchantKey` -> `merchants.js`
  (`merchantTokens` is an ordered array, `merchantMatches` -> `merchantSimilar`; keys of unknown merchants now drop the legal suffix, so re-key stored recurring groups once); `splits.js` -> `splitShares` / `settle`.
* **People** — `server/calendar.js` -> `buildIcs` (birthdays: `rrule: "FREQ=YEARLY"`, all-day, last day = same day); `server/dates.js addDays` -> `dates.addDays`.
* **Funes** — `timeparse.py` keeps its timestamp ranges; for plain "tomorrow / on Friday" phrases use `dates.parse_due`, and `ics`/`bizdays` for exports and deadlines.
* **Mercator** — `mercator.py parse_amount` (Decimal, `"1.234"` handling) and `parse_date`, `post_csv.py parse_when` -> `money.parse_amount(decimal=...)` and `dates.parse_date`.
* **Midas** — `client/src/format.js` stays in the browser (Intl); server-side exports and CSV amounts use `money.format_money` / `parse_cents` so figures match the other apps.
* **Cook** — `apps/web/src/format.js fmtMoney` and the ticket sum check in `packages/core/src/ticket.ts` -> `money.js` (`formatMoney`, `parseCents`; the sum compares cents, not floats).
* **Pygmalion** — `datasets/pii.py` (`luhn`, `iban_ok`, `scan`, `mask`, `counts`) -> `idcheck` (`scan_pii`, `mask_text`; checksum-validated hits, so counts may drop for look-alike numbers).
* **Hub** — `hub/purchases.py` `order_key`, `merchant_tokens`, `merchant_similar`, `_to_amount`, `_to_date`, `fmt_amount` -> `merchants` / `money` / `dates` (`merchant_tokens` is an ordered list: wrap in `set()`;
  tokens are 3+ characters now); `hub/ui/purchases.js money()` and `today.js` keep browser `Intl` formatting.
* **Faustus** — `routes/calendar_routes.py` `_ics_escape` and `export_ics` -> `ics.ics_escape` / `build_ics(events, name=cal.name, now=...)` (stored all-day DTEND is exclusive: `end_exclusive: True`; `is_utc` events pass
  an ISO string ending in `Z`; `RRULE` is passed through; gains DTSTAMP, folding and VALARM). The import side can use `parse_ics` for the common fields.
