"""Deterministic regional notes: accounting facts and separately scoped context.

Only reviewed, selected-vintage inputs are accepted. No causal inference,
interpolation, production-index subtraction, or regional ranking is performed.
Claims retain unrounded values and the evidence of every operand.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from .fiscal_analysis import (
    BASES, _observations, spending_function_history, transfer_history,
)
from .fiscal_diagnostics import revenue_component_history
from .fuel_prices import CORE_GRADES, LINEAGE, load_fuel_candidate, validate_fuel_prices

LIMITATIONS = (
    "Budget amounts are nominal; regional CPI and wage data are not included.",
    "Budget scope is regional + municipal, excluding territorial health-insurance funds.",
    "Accounting changes do not establish causes, service outcomes, or sanctions effects.",
    "An actual deficit is not a revenue shortfall against an approved budget plan.",
    "Transfers are from other budgets; exclusively federal origin is not established.",
    "Fuel prices are end-of-month RUB/litre observations, not CPI or period averages.",
    "Total tax receipts and industrial production have different economic perimeters.",
)


@dataclass
class NoteData:
    fiscal: pd.DataFrame
    components: pd.DataFrame
    functions: pd.DataFrame
    transfers: pd.DataFrame
    production: pd.DataFrame
    fuel: pd.DataFrame


@dataclass
class RegionalNote:
    region_id: str
    region_name: str
    period: str
    amount_basis: str
    window: str
    paragraphs: list[str]
    claims: list[dict]
    sources: list[dict]

    def markdown(self) -> str:
        text = [f"# {self.region_name} — {self.window} {self.period[:4]}",
                "Research review candidate · descriptive analysis · not independently verified", ""]
        text.extend(f"- {paragraph}" for paragraph in self.paragraphs)
        text.extend(["", "## Interpretation limits", ""])
        text.extend(f"- {item}" for item in LIMITATIONS)
        text.extend(["", "## Sources and vintages", ""])
        groups = {}
        for source in self.sources:
            key = (source.get("source_file"), source.get("source_sha256"),
                   source.get("reporting_cutoff") or source.get("period_end") or source.get("period"), source.get("source_vintage"))
            location = ":".join(str(source[f]) for f in ("source_member", "source_sheet", "source_cell") if source.get(f))
            groups.setdefault(key, set()).add(location)
        for (file, sha, cutoff, vintage), locations in groups.items():
            text.append(f"- {file} · {', '.join(sorted(locations))} · observation/cutoff {cutoff or 'not recorded'}"
                        f" · vintage {vintage or 'not recorded'} · SHA-256 {sha or 'not recorded'}")
        text.extend(["", "Exact operand evidence and row/cell locators are preserved in the companion JSON/claims export."])
        return "\n".join(text) + "\n"

    def as_dict(self) -> dict:
        return dict(region_id=self.region_id, region_name=self.region_name,
                    period=self.period, amount_basis=self.amount_basis, window=self.window,
                    quality_status="candidate_not_promoted", paragraphs=self.paragraphs,
                    claims=self.claims, sources=self.sources, limitations=list(LIMITATIONS))


def _production(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    fields = {"region_id", "period", "okved_section", "window_start", "window_months",
              "frequency", "index_measure", "index_value", "growth_yoy_pct",
              "source_file", "source_sha256", "source_sheet", "source_cell", "source_vintage"}
    if not fields.issubset(frame):
        raise ValueError("Regional notes require cumulative production fields and lineage")
    data = frame.copy()
    data["period"] = pd.to_datetime(data.period, errors="raise")
    data["window_start"] = pd.to_datetime(data.window_start, errors="raise")
    if (data[["region_id", "period", "okved_section"]].isna().any().any()
            or data.duplicated(["region_id", "period", "okved_section"]).any()
            or not data.frequency.eq("YTD").all()
            or not data.index_measure.eq("same_period_previous_year_pct").all()
            or not data.period.dt.is_month_end.all()
            or not data.window_start.eq(pd.to_datetime(data.period.dt.year.astype(str) + "-01-01")).all()
            or not data.window_months.eq(data.period.dt.month).all()
            or not data.okved_section.isin(["TOTAL", "B", "C", "D", "E"]).all()):
        raise ValueError("Invalid production grain, perimeter or January-to-month basis")
    values = pd.to_numeric(data.index_value, errors="raise")
    if (np.isinf(values).any() or values.dropna().lt(0).any()
            or not np.allclose(data.growth_yoy_pct, values - 100, equal_nan=True)
            or not data.source_sha256.astype(str).str.fullmatch(r"[0-9a-f]{64}").all()
            or data[["source_file", "source_sheet", "source_cell", "source_vintage"]].isna().any().any()):
        raise ValueError("Invalid production value, growth or evidence")
    data["index_value"] = values
    return data


def prepare_note_data(fiscal, revenue, production=None, fuel=None) -> NoteData:
    """Prepare both bases once; reusable for every eligible region and window."""
    return NoteData(
        pd.concat([_observations(fiscal, basis) for basis in BASES], ignore_index=True),
        pd.concat([revenue_component_history(revenue, basis) for basis in BASES], ignore_index=True),
        pd.concat([spending_function_history(fiscal, basis) for basis in BASES], ignore_index=True),
        pd.concat([transfer_history(revenue, basis) for basis in BASES], ignore_index=True),
        _production(production),
        validate_fuel_prices(fuel) if fuel is not None and not fuel.empty else pd.DataFrame(),
    )


def _decode(value):
    if isinstance(value, str):
        return json.loads(value)
    return None if value is None or (np.isscalar(value) and pd.isna(value)) else value


def _sources(evidence) -> list[dict]:
    if isinstance(evidence, dict):
        if "source_file" in evidence:
            return [evidence]
        return [s for value in evidence.values() for s in _sources(value)]
    if isinstance(evidence, list):
        return [s for value in evidence for s in _sources(value)]
    return []


def _row_evidence(row: pd.Series, fields) -> dict:
    result = {}
    for field in fields:
        value = row.get(field)
        if value is not None and not pd.isna(value):
            result[field] = (value.strftime("%Y-%m-%d") if isinstance(value, pd.Timestamp)
                             else value.item() if isinstance(value, np.generic) else value)
    return result


def make_regional_note(data: NoteData, region_id: str, period, basis="YTD",
                       region_name: str | None = None) -> RegionalNote:
    """Describe one exact window, preserving unavailable claims with reasons."""
    date = pd.Timestamp(period)
    if (basis not in BASES or date.tz is not None or not date.is_quarter_end
            or date.month not in (3, 6)):
        raise ValueError("Notes support exact Q1/H1 endpoints and Q1/Q2 flows only")
    window = "Q1" if date.month == 3 else ("H1" if basis == "YTD" else "Q2")
    note = RegionalNote(region_id, region_name or region_id, date.strftime("%Y-%m-%d"), basis, window, [], [], [])
    prior = date - pd.DateOffset(years=1)

    def claim(code, value, units, formula, evidence, reason="", **extra):
        available = value is not None and pd.notna(value) and np.isfinite(value)
        note.claims.append(dict(claim_code=code, value=float(value) if available else None,
                                units=units, formula=formula, availability="available" if available else "unavailable",
                                reason=(reason or "Required source value or positive denominator unavailable") if not available else "",
                                evidence=evidence, **extra))
        note.sources.extend(_sources(evidence))

    def choose(frame):
        return frame.loc[frame.region_id.eq(region_id) & frame.period.eq(date)
                         & frame.amount_basis.eq(basis)] if not frame.empty else frame

    fiscal = choose(data.fiscal)
    totals = fiscal.loc[fiscal.kind.eq("summary")].set_index("metric_code")
    previous = data.fiscal.loc[data.fiscal.region_id.eq(region_id) & data.fiscal.period.eq(prior)
                              & data.fiscal.amount_basis.eq(basis) & data.fiscal.kind.eq("summary")].set_index("metric_code")
    current_values, changes = {}, {}
    for code in ("revenue_total", "expenditure_total", "budget_balance"):
        current = totals.loc[code] if code in totals.index else None
        old = previous.loc[code] if code in previous.index else None
        value = current.amount_rub if current is not None else None
        old_value = old.amount_rub if old is not None else None
        evidence = {"current": _decode(current.source_evidence) if current is not None else None,
                    "prior": _decode(old.source_evidence) if old is not None else None}
        change = value - old_value if pd.notna(value) and pd.notna(old_value) else None
        current_values[code], changes[code] = value, change
        claim(code, value, "RUB", f"{code}: selected {window} actual", {"current": evidence["current"]},
              "Current window or required flow endpoint unavailable")
        claim(code + "_change", change, "RUB", "current − same window one year earlier", evidence,
              "Current or prior-year matching window unavailable")
    complete = all(pd.notna(v) for v in current_values.values())
    if complete and abs(current_values["revenue_total"] - current_values["expenditure_total"] - current_values["budget_balance"]) > .02:
        raise ValueError("Note budget totals do not reconcile")
    compared = all(pd.notna(v) for v in changes.values())
    if compared and abs(changes["revenue_total"] - changes["expenditure_total"] - changes["budget_balance"]) > .02:
        raise ValueError("Note balance changes do not reconcile")
    if complete:
        balance = current_values["budget_balance"]
        position = "surplus" if balance > 0 else ("deficit" if balance < 0 else "balanced budget")
        paragraph = (f"Fiscal account: revenue {current_values['revenue_total']/1e9:,.2f} bn RUB; "
                     f"expenditure {current_values['expenditure_total']/1e9:,.2f} bn RUB; "
                     f"{position} {abs(balance)/1e9:,.2f} bn RUB.")
        paragraph += (f" Against {window} {prior.year}, balance changed {changes['budget_balance']/1e9:+,.2f} bn RUB "
                      f"(revenue {changes['revenue_total']/1e9:+,.2f}; expenditure {changes['expenditure_total']/1e9:+,.2f})."
                      if compared else " The prior-year matching-window comparison is unavailable.")
    else:
        paragraph = "Fiscal account: current-window totals are incomplete; no balance assessment is made."
    note.paragraphs.append(paragraph)

    components = choose(data.components)
    component_complete = len(components) == 5 and components.comparison_complete.all()
    # The narrative must not mix fiscal and revenue datasets with different totals or endpoints.
    for row in components.itertuples(index=False):
        claim("component_" + row.component_code, row.change_yoy_rub, "RUB", "same-window revenue component change",
              {"current": _decode(row.source_evidence), "prior": _decode(row.prior_source_evidence)},
              "Required revenue code or prior-year endpoint unavailable")
    if not components.empty and complete:
        if components.total_revenue_rub.notna().any() and abs(components.total_revenue_rub.dropna().iloc[0] - current_values["revenue_total"]) > .02:
            raise ValueError("Note fiscal and revenue totals disagree")
        for total_rows, field in ((totals, "total_source_evidence"), (previous, "prior_total_source_evidence")):
            if "revenue_total" not in total_rows.index or pd.isna(total_rows.loc["revenue_total"].amount_rub):
                continue
            summary = _decode(total_rows.loc["revenue_total"].source_evidence)
            for evidence in components[field]:
                other = _decode(evidence)
                if other is None:
                    continue
                for endpoint in ("start", "end"):
                    a, b = summary.get(endpoint), other.get(endpoint)
                    if (a is None) != (b is None) or (a is not None and any(a.get(f) != b.get(f) for f in
                        ("source_file", "source_sha256", "source_member", "reporting_cutoff"))):
                        raise ValueError("Note fiscal and revenue provenance disagrees")
    if component_complete and compared:
        if abs(components.change_yoy_rub.sum() - changes["revenue_total"]) > .02:
            raise ValueError("Note component changes do not reconcile to revenue change")
        ordered = components.sort_values(["change_yoy_rub", "component_code"])
        positive, negative = ordered.loc[ordered.change_yoy_rub.gt(0)], ordered.loc[ordered.change_yoy_rub.lt(0)]
        parts = []
        if not positive.empty:
            row = positive.iloc[-1]
            parts.append(f"largest increase: {row.component_name} ({row.change_yoy_rub/1e9:+,.2f} bn RUB)")
        if not negative.empty:
            row = negative.iloc[0]
            parts.append(f"largest decrease: {row.component_name} ({row.change_yoy_rub/1e9:+,.2f} bn RUB)")
        note.paragraphs.append("Revenue components: " + ("; ".join(parts) if parts else "all five changes are zero")
                               + ". These are accounting contributions, not explanations of causation.")
    else:
        note.paragraphs.append("Revenue components: the complete matching-year decomposition is unavailable.")

    transfers = choose(data.transfers)
    if not transfers.empty:
        row = transfers.iloc[0]
        evidence = {"current_transfers": _decode(row.transfers_source_evidence),
                    "current_revenue": _decode(row.total_revenue_source_evidence),
                    "prior_transfers": _decode(row.prior_transfers_source_evidence),
                    "prior_revenue": _decode(row.prior_total_revenue_source_evidence)}
        claim("transfer_share", row.transfer_share_pct, "%", "code 202 / total revenue × 100", evidence)
        claim("transfer_share_change", row.transfer_share_change_pp, "pp", "current share − prior same-window share", evidence,
              "Matching transfer or revenue denominator unavailable")
        if pd.notna(row.transfer_share_pct):
            change = f" ({row.transfer_share_change_pp:+.2f} pp YoY)" if pd.notna(row.transfer_share_change_pp) else " (YoY unavailable)"
            note.paragraphs.append(f"Transfers from other budgets: {row.transfer_share_pct:.2f}% of revenue{change}.")
    functions = choose(data.functions)
    if len(functions) == 14 and functions.comparison_complete.all():
        ordered = functions.sort_values(["change_yoy_rub", "metric_code"])
        candidates = [ordered.loc[ordered.change_yoy_rub.gt(0)].tail(1), ordered.loc[ordered.change_yoy_rub.lt(0)].head(1)]
        parts = []
        for selected in candidates:
            if selected.empty:
                continue
            row = selected.iloc[0]
            claim("spending_" + row.metric_code, row.change_yoy_rub, "RUB", "same-window spending-function change",
                  {"current": _decode(row.source_evidence), "prior": _decode(row.prior_source_evidence)})
            parts.append(f"{row.function_name_en} {row.change_yoy_rub/1e9:+,.2f} bn RUB")
        note.paragraphs.append("Largest spending-function movements: " + ("; ".join(parts) if parts else "no changes")
                               + ". Classification amounts do not establish changes in service provision.")
    else:
        note.paragraphs.append("Spending-function movements: complete matching-year coverage is unavailable.")

    production = data.production
    selected = (production.loc[production.region_id.eq(region_id) & production.period.eq(date)]
                if not production.empty else pd.DataFrame())
    if window == "Q2":
        selected = pd.DataFrame()
        reason = "Standalone Q2 output cannot be recovered by subtracting cumulative indices."
    else:
        reason = "The exact published January-to-month observation is unavailable."
    parts, missing_production = [], []
    for section, label in (("TOTAL", "headline industrial production"), ("C", "manufacturing")):
        rows = selected.loc[selected.okved_section.eq(section)] if not selected.empty else selected
        row = rows.iloc[0] if len(rows) else None
        value = row.index_value - 100 if row is not None and pd.notna(row.index_value) else None
        evidence = _row_evidence(row, ("period", "window_start", "window_months", "source_file", "source_sha256",
                                       "source_sheet", "source_cell", "source_vintage", "source_url", "index_base")) if row is not None else {}
        claim("production_" + section, value, "%", "published same-window index − 100", evidence, reason,
              economic_scope=section)
        if pd.notna(value):
            parts.append(f"{label} {value:+.1f}% YoY")
        else:
            missing_production.append(f"{label} unavailable")
    note.paragraphs.append((f"Production context ({window} {date.year}): " + "; ".join(parts + missing_production)
                            + ". This is a separate output measure, not a direct cross-check of all tax receipts.")
                           if parts else "Production context: unavailable. " + reason)

    parts = []
    month = date.replace(day=1)
    old_month = month - pd.DateOffset(years=1)
    for grade in CORE_GRADES:
        fuel = data.fuel
        prices = fuel.loc[fuel.region_id.eq(region_id) & fuel.fuel_grade.eq(grade)] if not fuel.empty else fuel
        rows = prices.loc[prices.period.eq(month)] if not prices.empty else prices
        old_rows = prices.loc[prices.period.eq(old_month)] if not prices.empty else prices
        row, old = (rows.iloc[0] if len(rows) else None), (old_rows.iloc[0] if len(old_rows) else None)
        value = row.price_rub_per_litre if row is not None else None
        old_value = old.price_rub_per_litre if old is not None else None
        change = (value / old_value - 1) * 100 if pd.notna(value) and pd.notna(old_value) else None
        fields = ("period", "period_end", *LINEAGE)
        evidence = {"current": _row_evidence(row, fields) if row is not None else None,
                    "prior": _row_evidence(old, fields) if old is not None else None}
        claim("fuel_" + grade + "_price", value, "RUB/litre", "reported end-of-month retail price", {"current": evidence["current"]},
              "Exact endpoint fuel price unavailable")
        claim("fuel_" + grade + "_yoy", change, "%", "(current / same-month prior-year price − 1) × 100", evidence,
              "Exact endpoint or same-month prior-year price unavailable")
        if pd.notna(value):
            parts.append(f"{grade} {value:.2f} RUB/litre" + (f" ({change:+.1f}% YoY)" if pd.notna(change) else " (YoY unavailable)"))
        else:
            parts.append(f"{grade} unavailable")
    note.paragraphs.append(f"Fuel context (end-{date:%B %Y}): " + ("; ".join(parts) if parts else "unavailable")
                           + ". Endpoint prices are not an average for the fiscal window or a measure of total inflation.")
    unique = {json.dumps(s, ensure_ascii=False, sort_keys=True, default=str): s for s in note.sources}
    note.sources = [unique[key] for key in sorted(unique)]
    return note


def load_dashboard_note_fuel(project_root: Path, region_ids) -> pd.DataFrame | None:
    """Use the same checked release/explicit override as the Streamlit entrypoint.

    The existing dashboard bundle does not include its separately loaded fuel
    table. This adapter avoids an implicit search of raw files or a fallback to
    another vintage; errors propagate to the optional note's unavailable state.
    """
    from .cloud_release import resolve_dashboard_config
    config = resolve_dashboard_config(project_root, os.environ)
    root = os.environ.get("MACRO_RUS_FUEL_CANDIDATE_DIR") or config.fuel_root
    return load_fuel_candidate(Path(root), eligible_region_ids=region_ids)[0] if root else None


def render_regional_note(st, fiscal, revenue, region_id, period, basis, *, fuel=None, fuel_error=None):
    """One collapsed, filter-scoped note with exact claim and source downloads."""
    regional = fiscal.loc[fiscal.region_id.eq(region_id)]
    if isinstance(revenue, dict):
        subset = {name: frame.loc[frame.region_id.eq(region_id)] if isinstance(frame, pd.DataFrame) and "region_id" in frame
                  else frame for name, frame in revenue.items()}
        production = subset.get("industrial_production_cumulative")
    else:
        subset, production = revenue.loc[revenue.region_id.eq(region_id)], None
    region_name = regional.region_name_ru.iloc[0] if "region_name_ru" in regional and len(regional) else region_id
    data = prepare_note_data(regional, subset, production,
                             fuel.loc[fuel.region_id.eq(region_id)] if fuel is not None else None)
    note = make_regional_note(data, region_id, period, basis, region_name)
    with st.expander("Regional analytical note", expanded=False):
        st.caption(f"{note.region_name} · {note.window} {note.period[:4]} · research review candidate")
        for paragraph in note.paragraphs:
            st.markdown("- " + paragraph)
        st.caption("All fiscal amounts are nominal. An actual deficit is not a plan-based shortfall. No causal sanctions claim is made.")
        if fuel_error:
            st.caption(f"Fuel context withheld: {fuel_error}")
        vintages = sorted({s["source_vintage"] for s in note.sources if s.get("window_months") and s.get("source_vintage")})
        st.caption("Production source vintage: " + (", ".join(vintages) if vintages else "unavailable") +
                   ". Fuel publication/retrieval dates: not recorded in existing metadata.")
        st.caption("Sources: Federal Treasury fiscal reports; Rosstat industrial production and retail fuel price workbooks. "
                   "File vintages, cutoffs, cells, hashes, formulas and missing-data reasons are in the downloads.")
        prefix = f"regional_note_{region_id}_{note.period}_{basis}"
        for label, content, extension, mime in (
            ("Download regional note", note.markdown().encode(), "md", "text/markdown"),
            ("Download exact claims and sources", json.dumps(note.as_dict(), ensure_ascii=False, indent=2, allow_nan=False).encode(), "json", "application/json"),
        ):
            st.download_button(label, content, file_name=f"{prefix}.{extension}", mime=mime, key=f"{prefix}_{extension}")
