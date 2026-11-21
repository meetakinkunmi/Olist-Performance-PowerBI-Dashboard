"""
validate.py - independent check of the Olist Power BI dashboard figures.

It re-creates every cleaning step done in Power Query, then recomputes the DAX
measures with pandas, so the numbers printed here should match the report.

Usage
-----
    pip install pandas numpy
    python report/scripts/validate.py --data data
    python validate.py --data data --csv validation_results.csv     # also save the table

Two columns of results are produced, because the report can be read two ways:

  ALL TIME
      Matches Power BI when NO date filter is active. Use a scratch page, or
      temporarily remove Date[In Window] from "Filters on all pages".

  DASHBOARD WINDOW (2017-01-01 to 2018-08-31)
      Matches the four report pages as published, with no slicers applied.
      The report keeps Date[In Window] = 1 on every page.

Repeat Rate (lifetime) removes the Date filter (ALL('Date')), so it equals the
ALL TIME value on every page.

Cleaning replicated from Power Query
------------------------------------
  orders         dates (not timestamps) for delivery logic; customer_unique_id merged
                 in; DeliveryDays, PromisedDays, DaysVsEstimate, IsLate,
                 DeliveryStatus, IsValidSale (0 for canceled / unavailable)
  order_reviews  one review per order: newest review_answer_timestamp wins
  order_payments rows with payment_type = not_defined removed
  products       English category, 2 missing translations added, blank renamed to Uncategorized
  geolocation    inside Brazil only, one row per zip prefix with average lat / lng
  customers      state name and region merged in, coordinates merged in
And from the model / DAX:
  OrderType      "First order" if the order timestamp equals the customer's earliest
                 order timestamp over ALL orders (calculated column, no filters)

One modelling detail is reproduced on purpose: delivery and review measures wrap
CROSSFILTER(order_items[order_id], orders[order_id], BOTH). When a date filter is
active, that keeps only orders that have at least one item, so the DASHBOARD WINDOW
column applies the same rule. With no filter at all, nothing is restricted.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ENC = "utf-8-sig"  # also strips a byte order mark if a file has one
WINDOW_START = pd.Timestamp("2017-01-01")
WINDOW_END = pd.Timestamp("2018-08-31")

STATES = {
    "AC": ("Acre", "North"), "AL": ("Alagoas", "Northeast"), "AM": ("Amazonas", "North"),
    "AP": ("Amapá", "North"), "BA": ("Bahia", "Northeast"), "CE": ("Ceará", "Northeast"),
    "DF": ("Distrito Federal", "Central-West"), "ES": ("Espírito Santo", "Southeast"),
    "GO": ("Goiás", "Central-West"), "MA": ("Maranhão", "Northeast"),
    "MG": ("Minas Gerais", "Southeast"), "MS": ("Mato Grosso do Sul", "Central-West"),
    "MT": ("Mato Grosso", "Central-West"), "PA": ("Pará", "North"),
    "PB": ("Paraíba", "Northeast"), "PE": ("Pernambuco", "Northeast"),
    "PI": ("Piauí", "Northeast"), "PR": ("Paraná", "South"),
    "RJ": ("Rio de Janeiro", "Southeast"), "RN": ("Rio Grande do Norte", "Northeast"),
    "RO": ("Rondônia", "North"), "RR": ("Roraima", "North"),
    "RS": ("Rio Grande do Sul", "South"), "SC": ("Santa Catarina", "South"),
    "SE": ("Sergipe", "Northeast"), "SP": ("São Paulo", "Southeast"),
    "TO": ("Tocantins", "North"),
}

EXTRA_TRANSLATIONS = {
    "pc_gamer": "pc gamer",
    "portateis_cozinha_e_preparadores_de_alimentos": "portable kitchen and food preparers",
}

EXPECTED_ROWS = {
    "orders": 99_441,
    "order_items": 112_650,
    "order_payments (cleaned)": 103_883,
    "order_reviews (cleaned)": 98_673,
    "customers": 99_441,
    "sellers": 3_095,
    "products": 32_951,
}


# --------------------------------------------------------------------------- loading
def read(folder: Path, name: str, **kw) -> pd.DataFrame:
    path = folder / name
    if not path.exists():
        sys.exit(f"Missing file: {path}")
    return pd.read_csv(path, encoding=ENC, **kw)


def load_and_clean(folder: Path) -> dict[str, pd.DataFrame]:
    # ---- geolocation: Brazil only, one row per zip prefix (average coordinate)
    geo = read(folder, "olist_geolocation_dataset.csv",
               dtype={"geolocation_zip_code_prefix": str})
    geo = geo[geo.geolocation_lat.between(-34, 6) & geo.geolocation_lng.between(-74, -34)]
    geo = (geo.groupby("geolocation_zip_code_prefix", as_index=False)
              .agg(lat=("geolocation_lat", "mean"), lng=("geolocation_lng", "mean")))

    # ---- customers: state name, region, coordinates
    cust = read(folder, "olist_customers_dataset.csv", dtype=str)
    n0 = len(cust)
    cust["state_name"] = cust.customer_state.map(lambda s: STATES.get(s, (None, None))[0])
    cust["region"] = cust.customer_state.map(lambda s: STATES.get(s, (None, None))[1])
    cust = cust.merge(geo, left_on="customer_zip_code_prefix",
                      right_on="geolocation_zip_code_prefix", how="left")
    assert len(cust) == n0, "geolocation merge multiplied customer rows"

    # ---- sellers
    sellers = read(folder, "olist_sellers_dataset.csv", dtype=str)

    # ---- products: English category, missing translations added, blank renamed to Uncategorized
    trans = read(folder, "product_category_name_translation.csv", dtype=str)
    trans = pd.concat([
        trans,
        pd.DataFrame({"product_category_name": list(EXTRA_TRANSLATIONS),
                      "product_category_name_english": list(EXTRA_TRANSLATIONS.values())}),
    ], ignore_index=True)
    prods = read(folder, "olist_products_dataset.csv", dtype={"product_id": str,
                                                                 "product_category_name": str})
    n0 = len(prods)
    prods = prods.merge(trans, on="product_category_name", how="left")
    assert len(prods) == n0, "translation merge multiplied product rows"
    raw = prods.product_category_name_english.fillna(prods.product_category_name)
    prods["category"] = raw.str.replace("_", " ", regex=False).str.title().fillna("Uncategorized")

    # ---- orders
    o = read(folder, "olist_orders_dataset.csv", dtype=str)
    for c in ["order_purchase_timestamp", "order_approved_at", "order_delivered_carrier_date",
              "order_delivered_customer_date", "order_estimated_delivery_date"]:
        o[c] = pd.to_datetime(o[c], errors="coerce")
    n0 = len(o)
    o = o.merge(cust[["customer_id", "customer_unique_id"]], on="customer_id", how="left")
    assert len(o) == n0, "customer merge multiplied order rows"

    o["PurchaseDate"] = o.order_purchase_timestamp.dt.normalize()
    o["DeliveredDate"] = o.order_delivered_customer_date.dt.normalize()
    o["EstimatedDate"] = o.order_estimated_delivery_date.dt.normalize()
    o["DeliveryDays"] = (o.DeliveredDate - o.PurchaseDate).dt.days
    o["PromisedDays"] = (o.EstimatedDate - o.PurchaseDate).dt.days
    o["DaysVsEstimate"] = (o.DeliveredDate - o.EstimatedDate).dt.days
    o["IsLate"] = np.where(o.DaysVsEstimate.isna(), np.nan, (o.DaysVsEstimate > 0).astype(float))
    o["DeliveryStatus"] = np.select([o.DaysVsEstimate.isna(), o.DaysVsEstimate > 0],
                                    ["Not delivered", "Late"], default="On time")
    o["IsValidSale"] = (~o.order_status.isin(["canceled", "unavailable"])).astype(int)
    first_ts = o.groupby("customer_unique_id").order_purchase_timestamp.transform("min")
    o["OrderType"] = np.where(o.order_purchase_timestamp == first_ts, "First order", "Repeat order")

    # ---- order_items
    items = read(folder, "olist_order_items_dataset.csv",
                 dtype={"order_id": str, "product_id": str, "seller_id": str})

    # ---- order_payments: drop not_defined, readable labels (Text.Proper -> title case)
    pay = read(folder, "olist_order_payments_dataset.csv", dtype={"order_id": str})
    pay = pay[pay.payment_type != "not_defined"].copy()
    pay["payment_type"] = pay.payment_type.str.replace("_", " ", regex=False).str.title()

    # ---- order_reviews: one per order, newest answer timestamp wins (stable sort)
    rev = read(folder, "olist_order_reviews_dataset.csv",
               dtype={"review_id": str, "order_id": str})
    rev["review_answer_timestamp"] = pd.to_datetime(rev.review_answer_timestamp, errors="coerce")
    rev = (rev.sort_values(["order_id", "review_answer_timestamp"], ascending=[True, False],
                           na_position="last", kind="mergesort")
              .drop_duplicates("order_id", keep="first"))
    rev["review_score"] = rev.review_score.astype(int)

    return dict(orders=o, items=items, pay=pay, rev=rev, prods=prods,
                cust=cust, sellers=sellers, geo=geo)


# --------------------------------------------------------------------------- measures
class Results:
    def __init__(self):
        self.rows: dict[str, tuple[float, str]] = {}

    def add(self, key: str, value: float, kind: str):
        self.rows[key] = (value, kind)


def compute(d: dict[str, pd.DataFrame], window: bool) -> tuple[Results, dict]:
    o, items, pay, rev = d["orders"], d["items"], d["pay"], d["rev"]
    prods, cust = d["prods"], d["cust"]
    r = Results()

    # Filter context (Date -> orders -> order_items)
    if window:
        o_date = o[(o.PurchaseDate >= WINDOW_START) & (o.PurchaseDate <= WINDOW_END)]
        items_s = items[items.order_id.isin(o_date.order_id)]
        o_pop = o_date[o_date.order_id.isin(items_s.order_id)]  # CROSSFILTER effect
    else:
        o_date, items_s, o_pop = o, items, o

    # ---- Sales (IsValidSale = 1)
    valid_ids = set(o_date.loc[o_date.IsValidSale == 1, "order_id"])
    vi = items_s[items_s.order_id.isin(valid_ids)]
    revenue, freight = vi.price.sum(), vi.freight_value.sum()
    orders_n, items_n = vi.order_id.nunique(), len(vi)

    r.add("Gross Item Value (all statuses)", items_s.price.sum(), "money")
    r.add("Revenue", revenue, "money")
    r.add("Freight", freight, "money")
    r.add("Orders", orders_n, "int")
    r.add("Items Sold", items_n, "int")
    r.add("AOV", revenue / orders_n, "money")
    r.add("Avg Item Price", revenue / items_n, "money")
    r.add("Items per Order", items_n / orders_n, "num")
    r.add("Freight % of Revenue", freight / revenue, "pct")

    # ---- Sellers
    seller_rev = vi.groupby("seller_id").price.sum()
    seller_rev = seller_rev[seller_rev > 0].sort_values(ascending=False)
    top_n = math.ceil(len(seller_rev) * 0.1)
    r.add("Active Sellers", vi.seller_id.nunique(), "int")
    r.add("Revenue per Seller", revenue / vi.seller_id.nunique(), "money")
    r.add("Top 10% Seller Share", seller_rev.head(top_n).sum() / revenue, "pct")

    # ---- Delivery (population: orders after the CROSSFILTER rule)
    deliv = o_pop[o_pop.DeliveryStatus != "Not delivered"]
    r.add("Avg Delivery Days", deliv.DeliveryDays.mean(), "num")
    r.add("Avg Promised Days", deliv.PromisedDays.mean(), "num")
    r.add("Late Orders", deliv.IsLate.sum(), "int")
    r.add("Late Delivery %", deliv.IsLate.sum() / deliv.IsLate.count(), "pct")
    r.add("Avg Days vs Estimate", deliv.DaysVsEstimate.mean(), "num")

    # ---- Reviews
    rv = rev[rev.order_id.isin(o_pop.order_id)].merge(
        o_pop[["order_id", "DeliveryStatus"]], on="order_id", how="left")
    on_time = rv.loc[rv.DeliveryStatus == "On time", "review_score"].mean()
    late = rv.loc[rv.DeliveryStatus == "Late", "review_score"].mean()
    r.add("Reviews", len(rv), "int")
    r.add("Avg Review Score", rv.review_score.mean(), "num")
    r.add("% 5-Star", (rv.review_score == 5).mean(), "pct")
    r.add("% Low Scores (1-2)", (rv.review_score <= 2).mean(), "pct")
    r.add("Avg Review Score - On time", on_time, "num")
    r.add("Avg Review Score - Late", late, "num")
    r.add("Review Gap (On time vs Late)", on_time - late, "num")

    # ---- Customers (valid orders, orders table in date context)
    ov = o_date[o_date.IsValidSale == 1]
    per_person = ov.groupby("customer_unique_id").size()
    unique_c = ov.customer_unique_id.nunique()
    r.add("Unique Customers", unique_c, "int")
    r.add("New Customers", ov.loc[ov.OrderType == "First order", "customer_unique_id"].nunique(), "int")
    r.add("Returning Customers", ov.loc[ov.OrderType == "Repeat order", "customer_unique_id"].nunique(), "int")
    r.add("Repeat Customers", int((per_person > 1).sum()), "int")
    r.add("Repeat Rate", (per_person > 1).sum() / unique_c, "pct")

    # Lifetime = ALL('Date'): identical on every page
    ov_all = o[o.IsValidSale == 1]
    pp_all = ov_all.groupby("customer_unique_id").size()
    r.add("Repeat Rate (lifetime)", (pp_all > 1).sum() / ov_all.customer_unique_id.nunique(), "pct")

    # ---- Payments (orders in date context; no item requirement)
    ps = pay[pay.order_id.isin(o_date.order_id)]
    r.add("Payment Value", ps.payment_value.sum(), "money")
    r.add("Paid Orders", ps.order_id.nunique(), "int")
    r.add("Avg Installments", ps.payment_installments.mean(), "num")
    mix = ps.groupby("payment_type").payment_value.sum() / ps.payment_value.sum()
    for name, share in mix.sort_values(ascending=False).items():
        r.add(f"Payment Mix % - {name}", share, "pct")

    # ---- detail tables for the dashboard visuals
    cat = (vi.merge(prods[["product_id", "category"]], on="product_id", how="left")
             .groupby("category").price.sum().sort_values(ascending=False))
    cum = cat.cumsum() / cat.sum()
    state = (vi.merge(o[["order_id", "customer_id"]], on="order_id")
               .merge(cust[["customer_id", "customer_state"]], on="customer_id")
               .groupby("customer_state").price.sum().sort_values(ascending=False))
    r.add("Top 10 categories share of Revenue", cat.head(10).sum() / revenue, "pct")
    r.add("Categories needed for 80% of Revenue", int((cum < 0.8).sum() + 1), "int")
    r.add("São Paulo (SP) share of Revenue", state.get("SP", 0) / revenue, "pct")

    return r, {"categories": cat, "states": state}


# --------------------------------------------------------------------------- printing
def fmt(v: float, kind: str) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    if kind == "money":
        return f"R$ {v:,.2f}"
    if kind == "int":
        return f"{v:,.0f}"
    if kind == "pct":
        return f"{v * 100:.2f}%"
    return f"{v:,.2f}"


def line(char="-", n=92):
    print(char * n)


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate Olist Power BI figures with pandas.")
    ap.add_argument("--data", default="data", help="folder containing the 9 Olist CSV files")
    ap.add_argument("--csv", default=None, help="optional path to save the comparison table")
    args = ap.parse_args()

    folder = Path(args.data)
    print(f"Reading and cleaning the 9 files from: {folder.resolve()}")
    d = load_and_clean(folder)
    failures = 0

    # ---- 1. Row counts after cleaning (Phase 2 checkpoint)
    print("\n1. ROW COUNTS AFTER CLEANING (Power Query checkpoint)")
    line()
    actual = {
        "orders": len(d["orders"]),
        "order_items": len(d["items"]),
        "order_payments (cleaned)": len(d["pay"]),
        "order_reviews (cleaned)": len(d["rev"]),
        "customers": len(d["cust"]),
        "sellers": len(d["sellers"]),
        "products": len(d["prods"]),
    }
    print(f"{'Table':<30}{'Python':>12}{'Expected':>12}   Status")
    for name, exp in EXPECTED_ROWS.items():
        ok = actual[name] == exp
        failures += 0 if ok else 1
        print(f"{name:<30}{actual[name]:>12,}{exp:>12,}   {'OK' if ok else 'CHECK'}")
    extras = {
        "Distinct customer_unique_id": (d["cust"].customer_unique_id.nunique(), 96_096),
        "Products labelled Uncategorized": ((d["prods"].category == "Uncategorized").sum(), 610),
    }
    for name, (val, exp) in extras.items():
        ok = val == exp
        failures += 0 if ok else 1
        print(f"{name:<30}{val:>12,}{exp:>12,}   {'OK' if ok else 'CHECK'}")
    by_year = d["orders"].PurchaseDate.dt.year.value_counts().sort_index()
    print("Orders by year (all statuses):",
          ", ".join(f"{y}: {n:,}" for y, n in by_year.items()), "(expected 2016: 329, 2017: 45,101, 2018: 54,011)")
    no_coords = d["cust"].customer_lat.isna().sum() if "customer_lat" in d["cust"] else d["cust"].lat.isna().sum()
    print(f"Customers without map coordinates: {no_coords:,} (a few hundred is expected)")
    print(f"Customers with no state match: {d['cust'].state_name.isna().sum():,} (expected 0)")

    # ---- 2. Measures
    all_res, _ = compute(d, window=False)
    win_res, win_detail = compute(d, window=True)

    print("\n2. MEASURES: compare each row with the matching Power BI card")
    line()
    print(f"{'Measure':<42}{'ALL TIME':>22}{'DASHBOARD WINDOW':>26}")
    line()
    table = []
    for key, (v_all, kind) in all_res.rows.items():
        v_win = win_res.rows.get(key, (float('nan'), kind))[0]
        print(f"{key:<42}{fmt(v_all, kind):>22}{fmt(v_win, kind):>26}")
        table.append({"measure": key, "all_time": v_all, "dashboard_window": v_win})
    line()
    print("Window = 2017-01-01 to 2018-08-31. Repeat Rate (lifetime) is the same on both by design.")

    # ---- 3. Sanity ranges from the guide (ALL TIME column)
    print("\n3. SANITY RANGES (ALL TIME)")
    line()
    checks = [
        ("Gross Item Value (all statuses)", 13_580_000, 13_600_000),
        ("Orders", 97_000, 99_000),
        ("Avg Review Score", 4.0, 4.2),
        ("Avg Delivery Days", 11.0, 13.5),
        ("Late Delivery %", 0.06, 0.09),
        ("Repeat Rate (lifetime)", 0.02, 0.04),
    ]
    for key, lo, hi in checks:
        v = all_res.rows[key][0]
        ok = lo <= v <= hi
        failures += 0 if ok else 1
        print(f"{key:<42}{fmt(v, all_res.rows[key][1]):>18}   range {lo:,} to {hi:,}   {'OK' if ok else 'CHECK'}")

    # ---- 4. Detail for the visuals (dashboard window)
    print("\n4. TOP 10 CATEGORIES BY REVENUE (dashboard window, matches Page 1 and 2)")
    line()
    cats = win_detail["categories"]
    total = cats.sum()
    for name, val in cats.head(10).items():
        print(f"{name:<40}{fmt(val, 'money'):>20}{val / total * 100:>9.1f}%")
    print("\n5. TOP 5 CUSTOMER STATES BY REVENUE (dashboard window, matches the Page 1 map)")
    line()
    for code, val in win_detail["states"].head(5).items():
        print(f"{code} - {STATES.get(code, ('?',))[0]:<30}{fmt(val, 'money'):>20}{val / total * 100:>9.1f}%")

    if args.csv:
        pd.DataFrame(table).to_csv(args.csv, index=False)
        print(f"\nSaved comparison table to {args.csv}")

    print("\nDone." if failures == 0 else f"\nDone. {failures} item(s) marked CHECK; "
          "see the notes in the file header (usually a cleaning step differs).")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())