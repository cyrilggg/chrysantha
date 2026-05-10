"""
Chrysantha data vendor for TradingAgents.

Implements all vendor functions required by TradingAgents' interface.py,
fetching data from Chrysantha's REST API where available, falling back to
yfinance for data Chrysantha doesn't store (fundamentals, news, etc.).
"""
from datetime import datetime, timedelta
from io import StringIO
from typing import Optional

import pandas as pd
import requests
from dateutil.relativedelta import relativedelta

# ── Chrysantha API client ──────────────────────────────────────────

_CHRYSANTHA_BASE = None
_CHRYSANTHA_TOKEN = None
_CHRYSANTHA_SESSION = None


def init_chrysantha_client(base_url: str, access_token: str):
    global _CHRYSANTHA_BASE, _CHRYSANTHA_TOKEN, _CHRYSANTHA_SESSION
    _CHRYSANTHA_BASE = base_url.rstrip("/")
    _CHRYSANTHA_TOKEN = access_token
    _CHRYSANTHA_SESSION = requests.Session()
    _CHRYSANTHA_SESSION.headers.update({
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    })


def _chrysantha_get(path: str, params: dict = None) -> Optional[dict]:
    """Call chrysantha API with error handling."""
    if not _CHRYSANTHA_SESSION:
        raise RuntimeError("Chrysantha client not initialized")
    try:
        resp = _CHRYSANTHA_SESSION.get(
            f"{_CHRYSANTHA_BASE}{path}",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


# ── Market data helpers ────────────────────────────────────────────

def _yf_fallback_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    """Fallback to yfinance for stock data."""
    import yfinance as yf
    ticker = yf.Ticker(symbol.upper())
    data = ticker.history(start=start_date, end=end_date)
    if data.empty:
        return f"No data found for symbol '{symbol}' between {start_date} and {end_date}"
    if data.index.tz is not None:
        data.index = data.index.tz_localize(None)
    for col in ["Open", "High", "Low", "Close", "Adj Close"]:
        if col in data.columns:
            data[col] = data[col].round(2)
    header = f"# Stock data for {symbol.upper()} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(data)}\n\n"
    return header + data.to_csv()


def _chrysantha_get_market_data(
    symbol: str, data_source: str, start_date: str, end_date: str
) -> pd.DataFrame:
    """Fetch market data from chrysantha API and return as DataFrame."""
    # Chrysantha endpoint: GET /market-data/:dataSource/:symbol
    result = _chrysantha_get(f"/market-data/{data_source}/{symbol}")
    if not result or "marketData" not in result:
        return pd.DataFrame()

    records = []
    for item in result.get("marketData", []):
        d = item.get("date", "")
        price = item.get("marketPrice")
        if d and price is not None:
            records.append({"Date": pd.Timestamp(d), "Close": float(price)})

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df = df.set_index("Date").sort_index()
    # Filter to date range
    mask = (df.index >= start_date) & (df.index <= end_date)
    return df[mask]


# ── Vendor function implementations ─────────────────────────────────

def chrysantha_get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    """Get OHLCV data from chrysantha, fallback to yfinance."""
    # Try chrysantha first (YAHOO data source for most stocks, MANUAL for A-shares)
    for ds in ("YAHOO", "MANUAL"):
        df = _chrysantha_get_market_data(symbol, ds, start_date, end_date)
        if not df.empty:
            header = f"# Stock data for {symbol.upper()} from {start_date} to {end_date}\n"
            header += f"# Data source: Chrysantha ({ds})\n"
            header += f"# Total records: {len(df)}\n\n"
            return header + df.to_csv()

    # Fallback to yfinance
    return _yf_fallback_stock_data(symbol, start_date, end_date)


def chrysantha_get_indicators(
    symbol: str, indicator: str, curr_date: str, look_back_days: int = 30
) -> str:
    """Compute technical indicators from chrysantha market data, fallback to yfinance."""
    start_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_date = (start_dt - timedelta(days=look_back_days * 3)).strftime("%Y-%m-%d")

    # Try to get data from chrysantha
    for ds in ("YAHOO", "MANUAL"):
        df = _chrysantha_get_market_data(symbol, ds, start_date, curr_date)
        if not df.empty and len(df) >= 2:
            try:
                result = _compute_indicator(df, indicator)
                if result:
                    return (
                        f"# {indicator.upper()} for {symbol.upper()}\n"
                        f"# Period: {start_date} to {curr_date}\n"
                        f"# Data source: Chrysantha ({ds})\n\n"
                        f"{result}"
                    )
            except Exception:
                pass

    # Fallback to yfinance
    from tradingagents.dataflows.y_finance import get_stock_stats_indicators_window
    return get_stock_stats_indicators_window(symbol, indicator, curr_date, look_back_days)


def _compute_indicator(df: pd.DataFrame, indicator: str) -> str:
    """Compute basic technical indicators from a price DataFrame."""
    close = df["Close"]
    indicator = indicator.lower().strip()

    if indicator == "rsi":
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        latest = rsi.iloc[-1]
        return f"RSI (14): {latest:.2f}\nLast 5 values:\n{rsi.tail(5).to_string()}"

    elif indicator in ("sma", "ema"):
        period = 20
        if indicator == "sma":
            val = close.rolling(window=period).mean()
        else:
            val = close.ewm(span=period, adjust=False).mean()
        latest = val.iloc[-1]
        return f"{indicator.upper()}({period}): {latest:.2f}\nClose: {close.iloc[-1]:.2f}"

    elif indicator == "macd":
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal = macd_line.ewm(span=9, adjust=False).mean()
        hist = macd_line - signal
        return (
            f"MACD Line: {macd_line.iloc[-1]:.4f}\n"
            f"Signal Line: {signal.iloc[-1]:.4f}\n"
            f"Histogram: {hist.iloc[-1]:.4f}"
        )

    elif indicator in ("boll", "bollinger"):
        sma20 = close.rolling(window=20).mean()
        std20 = close.rolling(window=20).std()
        upper = sma20 + 2 * std20
        lower = sma20 - 2 * std20
        return (
            f"Bollinger Bands (20,2):\n"
            f"Upper: {upper.iloc[-1]:.2f}\n"
            f"Middle: {sma20.iloc[-1]:.2f}\n"
            f"Lower: {lower.iloc[-1]:.2f}\n"
            f"Close: {close.iloc[-1]:.2f}"
        )

    elif indicator == "atr":
        return f"ATR requires OHLC data. Close: {close.iloc[-1]:.2f}"

    else:
        return f"Indicator '{indicator}' not supported by chrysantha vendor (close-only data). Try yfinance."


# ── Fundamental & news fallbacks ────────────────────────────────────

# These data types are not stored in chrysantha, so delegate to yfinance directly.
# The vendor routing will fall through to yfinance when chrysantha returns empty.


def chrysantha_get_fundamentals(ticker: str, curr_date: str) -> str:
    """Fundamentals: delegate to yfinance (not stored in chrysantha)."""
    from tradingagents.dataflows.y_finance import get_fundamentals
    return get_fundamentals(ticker, curr_date)


def chrysantha_get_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
    from tradingagents.dataflows.y_finance import get_balance_sheet
    return get_balance_sheet(ticker, freq, curr_date)


def chrysantha_get_cashflow(ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
    from tradingagents.dataflows.y_finance import get_cashflow
    return get_cashflow(ticker, freq, curr_date)


def chrysantha_get_income_statement(ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
    from tradingagents.dataflows.y_finance import get_income_statement
    return get_income_statement(ticker, freq, curr_date)


def chrysantha_get_news(ticker: str, start_date: str, end_date: str) -> str:
    from tradingagents.dataflows.yfinance_news import get_news_yfinance
    return get_news_yfinance(ticker, start_date, end_date)


def chrysantha_get_global_news(curr_date: str, look_back_days: int = 7, limit: int = 5) -> str:
    from tradingagents.dataflows.yfinance_news import get_global_news_yfinance
    return get_global_news_yfinance(curr_date, look_back_days, limit)


def chrysantha_get_insider_transactions(ticker: str) -> str:
    from tradingagents.dataflows.y_finance import get_insider_transactions
    return get_insider_transactions(ticker)


# ── Registration ────────────────────────────────────────────────────

def register_chrysantha_vendor(config: dict):
    """Dynamically register chrysantha as a data vendor in TradingAgents.

    Must be called BEFORE instantiating TradingAgentsGraph.
    Modifies TradingAgents' interface.py globals at runtime.
    """
    from tradingagents.dataflows.interface import VENDOR_METHODS, VENDOR_LIST

    # Register chrysantha implementations
    VENDOR_METHODS["get_stock_data"]["chrysantha"] = chrysantha_get_stock_data
    VENDOR_METHODS["get_indicators"]["chrysantha"] = chrysantha_get_indicators
    VENDOR_METHODS["get_fundamentals"]["chrysantha"] = chrysantha_get_fundamentals
    VENDOR_METHODS["get_balance_sheet"]["chrysantha"] = chrysantha_get_balance_sheet
    VENDOR_METHODS["get_cashflow"]["chrysantha"] = chrysantha_get_cashflow
    VENDOR_METHODS["get_income_statement"]["chrysantha"] = chrysantha_get_income_statement
    VENDOR_METHODS["get_news"]["chrysantha"] = chrysantha_get_news
    VENDOR_METHODS["get_global_news"]["chrysantha"] = chrysantha_get_global_news
    VENDOR_METHODS["get_insider_transactions"]["chrysantha"] = chrysantha_get_insider_transactions

    if "chrysantha" not in VENDOR_LIST:
        VENDOR_LIST.append("chrysantha")

    # Update config to use chrysantha as primary, yfinance as fallback
    config["data_vendors"] = {
        "core_stock_apis": "chrysantha,yfinance",
        "technical_indicators": "chrysantha,yfinance",
        "fundamental_data": "yfinance",       # chrysantha delegates to yfinance anyway
        "news_data": "yfinance",              # chrysantha doesn't store news
    }
