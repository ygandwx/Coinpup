"""Fixed HTTPS providers using only public pair and date parameters."""

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pydantic import TypeAdapter

from coinpup_api.ledger import AssetDefinition
from coinpup_api.quotes.schemas import PriceText
from coinpup_api.quotes.valuation import DirectQuote

ECB = "Frankfurter / ECB"
COINBASE = "Coinbase Exchange"
_MAX_RESPONSE = 32_768
_DISPLAY = frozenset({"USD", "GBP", "EUR", "HKD", "CNY"})
_COINBASE_PAIRS = frozenset(
    (base, display) for base in ("BTC", "ETH") for display in ("USD", "GBP", "EUR")
)
_PRICE = TypeAdapter(PriceText)


@dataclass(frozen=True, slots=True)
class MarketObservation:
    base: AssetDefinition
    display: AssetDefinition
    price: str
    source: str
    price_date: date
    observed_at: datetime | None
    fetched_at: datetime

    def __post_init__(self):
        if type(self.price_date) is not date or (self.source, self.observed_at is None) not in {
            (ECB, True),
            (COINBASE, False),
        }:
            raise ValueError("Market source and observation time differ.")
        canonical = self.direct_quote
        fetched = canonical.fetched_at
        if (
            self.price_date > fetched.date()
            or (self.observed_at is not None and canonical.effective_at.date() != self.price_date)
            or canonical.effective_at > fetched
        ):
            raise ValueError("Market observation is outside its declared time.")
        object.__setattr__(self, "fetched_at", fetched)
        if self.observed_at is not None:
            object.__setattr__(self, "observed_at", canonical.effective_at)

    @property
    def direct_quote(self):
        return DirectQuote(
            self.base,
            self.display,
            self.price,
            self.source,
            self.observed_at or datetime.combine(self.price_date, time.min, UTC),
            self.fetched_at,
        )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_market_response(url: str) -> bytes:
    endpoint = urlsplit(url)
    if (
        endpoint.scheme != "https"
        or endpoint.netloc not in {"api.frankfurter.dev", "api.exchange.coinbase.com"}
        or endpoint.fragment
    ):
        raise ValueError("Unapproved market provider endpoint.")
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "Coinpup quotes"})
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=8) as response:
        body = response.read(_MAX_RESPONSE + 1)
    if not 0 < len(body) <= _MAX_RESPONSE:
        raise ValueError("Market response is outside the allowed bounds.")
    return body


def _unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Repeated market response field.")
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError("Expected finite market data.")


def _parse(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= _MAX_RESPONSE:
        raise ValueError("Expected bounded market data.")
    try:
        return json.loads(
            raw.decode("utf-8"),
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (ValueError, InvalidOperation, RecursionError):
        raise ValueError("Invalid market provider response.") from None


def _fx_price(value):
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise ValueError("Expected an exact positive reference price.")
    # Check exponent and range before expanding scientific notation to a string.
    if not -18 <= value.adjusted() < 20 or value.as_tuple().exponent < -18:
        raise ValueError("Reference price is outside the allowed precision.")
    return _PRICE.validate_python(format(value, "f"))


def _fx_pair(base, display, day, fetch, clock):
    params = {"base": base.asset_id, "quotes": display.asset_id, "providers": "ecb"}
    if day is not None:
        params["date"] = day.isoformat()
    data = _parse(fetch("https://api.frankfurter.dev/v2/rates?" + urlencode(params)))
    if data == []:
        return None
    if type(data) is not list or len(data) != 1 or not isinstance(data[0], dict):
        raise ValueError("Expected exactly one requested reference pair.")
    row = data[0]
    if (row.get("base"), row.get("quote")) != (base.asset_id, display.asset_id):
        raise ValueError("Reference provider returned another pair.")
    if not isinstance(row.get("date"), str):
        raise ValueError("Reference provider is missing its day.")
    price_date = date.fromisoformat(row["date"])
    if price_date.isoformat() != row["date"]:
        raise ValueError("Reference provider returned a noncanonical day.")
    if day is not None and price_date != day:
        return None
    return MarketObservation(
        base, display, _fx_price(row.get("rate")), ECB, price_date, None, clock()
    )


def _ticker(base, display, fetch, clock):
    data = _parse(
        fetch(
            f"https://api.exchange.coinbase.com/products/{base.asset_id}-{display.asset_id}/ticker"
        )
    )
    if type(data) is not dict:
        raise ValueError("Expected one public trade ticker.")
    timestamp = data.get("time")
    if not isinstance(timestamp, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})",
        timestamp,
    ):
        raise ValueError("Public ticker is missing its exact zoned time.")
    observed = datetime.fromisoformat(timestamp).astimezone(UTC)
    return MarketObservation(
        base,
        display,
        _PRICE.validate_python(data.get("price")),
        COINBASE,
        observed.date(),
        observed,
        clock(),
    )


def fetch_market_observation(base, display, *, price_date=None, fetch, clock):
    """Query one approved direct pair; return no observation when the provider has no exact day."""
    if display.asset_id not in _DISPLAY or base == display:
        raise ValueError("Choose a distinct pair with a supported display currency.")
    if price_date is not None and type(price_date) is not date:
        raise ValueError("Choose an exact reference date.")
    if base.kind == "fiat":
        return _fx_pair(base, display, price_date, fetch, clock)
    if base.kind == "native" and (base.asset_id, display.asset_id) in _COINBASE_PAIRS:
        if price_date is not None:
            raise ValueError("The public spot ticker supplies only its current observation.")
        return _ticker(base, display, fetch, clock)
    return None
