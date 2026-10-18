"""Hand-worked accounting, exact-window context and scoped note-export checks."""
import json

import numpy as np
import pandas as pd
import pytest

from macro_rus.fiscal_analysis import SCOPE
from macro_rus.fiscal_view import FUNCTIONS
from macro_rus.fuel_prices import MEASURE, PRODUCT_GRADES
from macro_rus.regional_notes import make_regional_note, prepare_note_data, render_regional_note
from macro_rus.revenue_view import CIT, NON_REPAYABLE, OWN, PIT, TRANSFERS


@pytest.fixture
def raw():
    fiscal, revenue, production, fuel = [], [], [], []
    for region, multiple in (("RU-KLU", 1), ("RU-SVE", 2)):
        for period, total, own, pit, cit, transfers, spending in (
            ("2023-03-31", 100, 60, 30, 20, 35, 80),
            ("2023-06-30", 220, 150, 80, 40, 60, 200),
            ("2024-03-31", 120, 80, 45, 25, 30, 110),
            ("2024-06-30", 250, 180, 100, 50, 65, 240),
        ):
            date = pd.Timestamp(period)
            lineage = dict(region_id=region, region_name_ru=region, period=date, budget_level=SCOPE,
                           units="RUB", frequency="YTD", source_file=f"{date:%Y%m%d}.zip",
                           source_sha256="a" * 64, source_member=f"{region}.html",
                           reporting_cutoff=f"{date + pd.Timedelta(days=1):%Y-%m-%d}")
            for code, value in (("revenue_total", total), ("expenditure_total", spending), ("budget_balance", total-spending)):
                fiscal.append(dict(**lineage, metric_code=code, kind="summary", actual_ytd_rub=value*multiple*1e9))
            for code in FUNCTIONS:
                fiscal.append(dict(**lineage, metric_code=code, kind="expenditure_function", actual_ytd_rub=spending*multiple*1e9/14))
            for code, value in (("TOTAL_REVENUE", total), (OWN, own), (PIT, pit), (CIT, cit),
                                (NON_REPAYABLE, total-own), (TRANSFERS, transfers)):
                revenue.append(dict(**lineage, revenue_code=code, actual_ytd_rub=value*multiple*1e9))
            for section, index in (("TOTAL", 110), ("C", 80)):
                production.append(dict(region_id=region, period=date, okved_section=section, frequency="YTD",
                                       window_start=pd.Timestamp(date.year, 1, 1), window_months=date.month,
                                       index_measure="same_period_previous_year_pct", index_value=index,
                                       growth_yoy_pct=index-100, source_file="production.xlsx", source_sha256="b"*64,
                                       source_sheet="2", source_cell="C6", source_vintage="2024-08-01"))
        for period, price in (("2023-06-01", 50), ("2024-06-01", 55), ("2024-07-01", 999)):
            month = pd.Timestamp(period)
            for grade in ("AI-92", "AI-95", "Diesel"):
                fuel.append(dict(region_id=region, period=month, fuel_grade=grade,
                                 source_geography_code="29000000000" if multiple == 1 else "65000000000",
                                 geography_name_ru=region, geography_scope="region", eligible_for_existing_dashboard=True,
                                 product_code=next(c for c, g in PRODUCT_GRADES.items() if g == grade),
                                 product_name_ru=grade, price_rub_per_litre=price*multiple,
                                 measurement_basis=MEASURE, source_file="fuel.xlsx", source_sha256="c"*64,
                                 source_sheet=month.strftime("%m(%Y)"), source_cell="B6", quality_flag="reported"))
    return tuple(pd.DataFrame(rows) for rows in (fiscal, revenue, production, fuel))


def claims(note):
    return {c["claim_code"]: c for c in note.claims}


@pytest.mark.parametrize("basis, balance, change, pit", [("YTD", 10, -10, 20), ("period_flow", 0, 0, 5)])
def test_hand_worked_signed_accounting(raw, basis, balance, change, pit):
    note = make_regional_note(prepare_note_data(*raw), "RU-KLU", "2024-06-30", basis)
    values = claims(note)
    assert values["budget_balance"]["value"] == balance*1e9
    assert values["budget_balance_change"]["value"] == change*1e9
    assert values["component_retained_pit"]["value"] == pit*1e9
    assert values["component_other_non_repayable"]["value"] == (-5 if basis == "YTD" else -10)*1e9
    assert values["transfer_share"]["value"] == pytest.approx((65/250 if basis == "YTD" else 35/130)*100)
    json.dumps(note.as_dict(), allow_nan=False)
    assert "plan-based" in note.markdown() or "approved budget" in note.markdown()


def test_production_exact_window_and_no_q2_subtraction(raw):
    prepared = prepare_note_data(*raw)
    cumulative = claims(make_regional_note(prepared, "RU-KLU", "2024-06-30"))
    assert cumulative["production_TOTAL"]["value"] == 10
    assert cumulative["production_C"]["value"] == -20
    quarter = claims(make_regional_note(prepared, "RU-KLU", "2024-06-30", "period_flow"))
    assert quarter["production_TOTAL"]["value"] is None
    assert "subtracting" in quarter["production_TOTAL"]["reason"]
    first = claims(make_regional_note(prepared, "RU-KLU", "2024-03-31", "period_flow"))
    assert first["production_TOTAL"]["value"] == 10


def test_exact_fuel_endpoint_not_latest_and_both_sources(raw):
    note = make_regional_note(prepare_note_data(*raw), "RU-KLU", "2024-06-30")
    row = claims(note)["fuel_AI-92_yoy"]
    assert row["value"] == pytest.approx(10)
    assert claims(note)["fuel_AI-92_price"]["value"] == 55  # not July's 999
    assert row["evidence"]["current"]["period_end"] == "2024-06-30"
    assert row["evidence"]["prior"]["period_end"] == "2023-06-30"
    assert "measure of total inflation" in note.markdown()


def test_missing_prior_and_context_are_not_zero_or_nan(raw):
    note = make_regional_note(prepare_note_data(raw[0], raw[1]), "RU-KLU", "2023-03-31")
    values = claims(note)
    assert values["budget_balance_change"]["value"] is None
    assert values["production_TOTAL"]["value"] is None
    assert values["fuel_Diesel_price"]["value"] is None
    assert values["budget_balance"]["value"] == 20e9
    assert all(c["reason"] for c in note.claims if c["availability"] == "unavailable")
    assert "NaN" not in json.dumps(note.as_dict(), allow_nan=False)


def test_fuel_gap_not_carried_forward(raw):
    fiscal, revenue, production, fuel = raw
    fuel = fuel.loc[~fuel.period.eq(pd.Timestamp("2023-06-01"))]
    note = make_regional_note(prepare_note_data(fiscal, revenue, production, fuel), "RU-KLU", "2024-06-30")
    assert claims(note)["fuel_AI-92_price"]["value"] == 55
    assert claims(note)["fuel_AI-92_yoy"]["value"] is None


def test_region_and_sources_are_scoped_and_inputs_immutable(raw):
    originals = [frame.copy(deep=True) for frame in raw]
    prepared = prepare_note_data(*raw)
    first = make_regional_note(prepared, "RU-KLU", "2024-06-30")
    other = make_regional_note(prepared, "RU-SVE", "2024-06-30")
    assert claims(other)["budget_balance"]["value"] == 2*claims(first)["budget_balance"]["value"]
    assert claims(other)["fuel_AI-92_price"]["value"] == 110
    assert not any(s.get("source_member") == "RU-SVE.html" for s in first.sources)
    for original, current in zip(originals, raw):
        pd.testing.assert_frame_equal(original, current)


@pytest.mark.parametrize("mutation", ["duplicate", "basis", "window", "growth", "scope", "hash"])
def test_bad_production_contract_rejected(raw, mutation):
    fiscal, revenue, production, fuel = raw
    production = production.copy()
    if mutation == "duplicate":
        production = pd.concat([production, production.iloc[[0]]])
    else:
        field, value = {"basis": ("frequency", "monthly"), "window": ("window_start", pd.Timestamp("2023-02-01")),
                        "growth": ("growth_yoy_pct", 9), "scope": ("okved_section", "F"), "hash": ("source_sha256", "bad")}[mutation]
        production.loc[0, field] = value
    with pytest.raises(ValueError):
        prepare_note_data(fiscal, revenue, production, fuel)


@pytest.mark.parametrize("year", [2023, 2024])
def test_fiscal_revenue_mixed_lineage_rejected_even_if_totals_equal(raw, year):
    fiscal, revenue, production, fuel = raw
    revenue = revenue.copy()
    revenue.loc[revenue.period.eq(pd.Timestamp(year, 6, 30)), "source_sha256"] = "d"*64
    with pytest.raises(ValueError, match="provenance"):
        make_regional_note(prepare_note_data(fiscal, revenue, production, fuel), "RU-KLU", "2024-06-30")


@pytest.mark.parametrize("date,basis", [("2024-06-01", "YTD"), ("2024-09-30", "YTD"), ("2024-06-30", "monthly")])
def test_unsupported_windows_rejected(raw, date, basis):
    with pytest.raises(ValueError):
        make_regional_note(prepare_note_data(*raw), "RU-KLU", date, basis)


class FakeStreamlit:
    def __init__(self):
        self.downloads, self.paragraphs, self.expanders = [], [], []
    def expander(self, label, **kwargs):
        self.expanders.append((label, kwargs))
        return self
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def caption(self, text):
        pass
    def markdown(self, text):
        self.paragraphs.append(text)
    def download_button(self, label, content, **kwargs):
        self.downloads.append((label, content, kwargs))


def test_render_collapsed_and_exports_follow_active_region_period_basis(raw):
    fiscal, revenue, _, fuel = raw
    st = FakeStreamlit()
    render_regional_note(st, fiscal, revenue, "RU-SVE", "2024-06-30", "period_flow", fuel=fuel)
    assert st.expanders == [("Regional analytical note", {"expanded": False})]
    assert len(st.downloads) == 2
    parsed = json.loads(st.downloads[1][1])
    assert parsed["region_id"] == "RU-SVE" and parsed["window"] == "Q2"
    assert parsed["amount_basis"] == "period_flow"
    assert "RU-SVE_2024-06-30_period_flow" in st.downloads[1][2]["file_name"]
    assert parsed["paragraphs"] == [p.removeprefix("- ") for p in st.paragraphs]
