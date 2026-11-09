import numpy as np
import pandas as pd
import pytest

from tradewise.costs import ZERO_COSTS, CostModel
from tradewise.data.ingest import ingest
from tradewise.data.market import DataError, MarketData, validate_ohlcv
from tradewise.data.providers import CSVProvider, SyntheticProvider, YahooProvider, get_provider
from tradewise.data.store import PriceStore
from tradewise.data.universe import Instrument, load_universe


def test_delivery_costs_on_one_lakh():
    c = CostModel()
    # STT 100 + NSE 3.07 + SEBI 0.10 + stamp 15 + GST 18% of 3.17
    assert c.buy_cost(100_000) == pytest.approx(100 + 3.07 + 0.1 + 15 + 0.18 * 3.17)
    assert c.sell_cost(100_000) == pytest.approx(100 + 3.07 + 0.1 + 0.18 * 3.17 + 15.34)
    assert c.buy_cost(0) == 0 and ZERO_COSTS.round_trip(1e5, 1e5) == 0


def test_brokerage_cap_and_validation():
    c = CostModel(brokerage_rate=0.001, brokerage_cap=20)
    assert c.brokerage(1_000_000) == 20 and c.brokerage(1_000) == 1
    with pytest.raises(ValueError):
        CostModel(stt_rate=-0.1)


def test_validate_cleans_bad_rows():
    df = pd.DataFrame({
        "Date": ["2024-01-02", "2024-01-01", "2024-01-03", "2024-01-03", "2024-01-04"],
        "Open": [10, 10, 10, 11, -1], "High": [11, 11, 9, 12, 5], "Low": [9, 9, 10, 10, 4],
        "Close": [10.5, 10.5, 10, 11.5, 4.5], "Volume": [100, None, 100, 100, 100],
    })
    out = validate_ohlcv(df, "X")
    assert list(out.index.strftime("%Y-%m-%d")) == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert out.loc["2024-01-03", "close"] == 11.5  # duplicate date: last wins
    assert out.loc["2024-01-01", "volume"] == 0
    with pytest.raises(DataError):
        validate_ohlcv(df.drop(columns=["Close"]), "X")
    with pytest.raises(DataError):
        validate_ohlcv(pd.DataFrame(), "X")


def test_market_alignment_and_slicing(synth):
    assert synth.close.shape == synth.open.shape
    assert synth.benchmark.index.equals(synth.dates)
    part = synth.truncate("2018-06-29")
    assert part.dates[-1] <= pd.Timestamp("2018-06-29")
    assert synth.bars("AAA").index.is_monotonic_increasing
    with pytest.raises(DataError):
        MarketData(synth.open, synth.high, synth.low, synth.close.iloc[:-1], synth.volume, synth.benchmark)


def test_synthetic_is_deterministic():
    a = SyntheticProvider(seed=3).fetch("ABC", "2020-01-01", "2020-12-31")
    b = SyntheticProvider(seed=3).fetch("ABC", "2020-01-01", "2020-12-31")
    pd.testing.assert_frame_equal(a, b)
    assert (a["high"] >= a[["open", "close"]].max(axis=1)).all()


def test_store_roundtrip_and_incremental_ingest(tmp_path):
    store = PriceStore(tmp_path / "p.sqlite")
    prov = SyntheticProvider(seed=1, start="2019-01-01", end="2020-12-31")
    inst = [Instrument("AAA", "A Ltd", "IT"), Instrument("BBB", "B Ltd", "Banks")]
    rep = ingest(store, prov, inst, "NIFTY50", start="2019-01-01", end="2020-06-30", workers=2)
    assert rep.ok and rep.updated["AAA"] > 100
    rep2 = ingest(store, prov, inst, "NIFTY50", start="2019-01-01", end="2020-12-31")
    assert rep2.ok and rep2.updated["AAA"] < rep.updated["AAA"]  # only new days (+1 week overlap)
    loaded = store.load_bars("AAA")
    expect = prov.fetch("AAA", "2019-01-01", "2020-12-31")
    np.testing.assert_allclose(loaded["close"].to_numpy(), expect["close"].to_numpy())
    md = store.load_market("NIFTY50")
    assert md.symbols == ["AAA", "BBB"] and md.sector_of("BBB") == "Banks"
    assert store.get_meta("provider") == "synthetic"
    with pytest.raises(LookupError):
        PriceStore(tmp_path / "empty.sqlite").load_market("NIFTY50")


def test_ingest_reports_failures(tmp_path):
    class Broken:
        name = "broken"

        def fetch(self, symbol, start, end):
            raise DataError("boom")

    rep = ingest(PriceStore(tmp_path / "p.sqlite"), Broken(), [Instrument("AAA", "A", "IT")], "NIFTY50")
    assert not rep.ok and set(rep.failed) == {"AAA", "NIFTY50"}


def test_csv_provider(tmp_path):
    df = SyntheticProvider(seed=2).fetch("XYZ", "2021-01-01", "2021-03-31").reset_index()
    df.columns = ["Date", "Open", "High", "Low", "Close", "Volume"]
    df["Adj Close"] = df["Close"]
    df.to_csv(tmp_path / "XYZ.csv", index=False)
    p = CSVProvider(tmp_path)
    assert p.available() == ["XYZ"]
    out = p.fetch("xyz", "2021-02-01", "2021-02-28")
    assert out.index.min() >= pd.Timestamp("2021-02-01") and len(out) > 15
    with pytest.raises(DataError):
        p.fetch("NOPE", "2021-01-01", "2021-12-31")
    with pytest.raises(DataError):
        get_provider("bogus")


def test_yahoo_provider_with_fake_module(monkeypatch):
    import sys
    import types

    raw = SyntheticProvider(seed=4).fetch("ABC", "2022-01-01", "2022-02-28")
    raw.columns = pd.MultiIndex.from_product([["Open", "High", "Low", "Close", "Volume"], ["ABC.NS"]])
    calls = {}

    def download(ticker, **kw):
        calls["ticker"], calls["kw"] = ticker, kw
        return raw

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=download))
    out = YahooProvider().fetch("ABC", "2022-01-01", "2022-02-28")
    assert calls["ticker"] == "ABC.NS" and calls["kw"]["auto_adjust"] is True
    assert list(out.columns) == ["open", "high", "low", "close", "volume"] and len(out) == len(raw)
    assert YahooProvider.ticker("NIFTY50") == "^NSEI"


def test_universe_loader(tmp_path):
    u = load_universe()
    assert len(u) == 40 and u[0].symbol == "RELIANCE"
    f = tmp_path / "u.csv"
    f.write_text("Company Name,Industry,Symbol,Series\nA,IT,aaa,EQ\nB,IT,BBB,BE\nA,IT,AAA,EQ\n")
    assert [i.symbol for i in load_universe(f)] == ["AAA"]
    with pytest.raises(DataError):
        load_universe(tmp_path / "missing.csv")
