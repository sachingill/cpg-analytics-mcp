"""Synthetic CPG dataset generator.

Builds a small but realistic health & hygiene CPG world in DuckDB:

  - sell-out (retailer EPOS) weekly sales with a known baseline
  - trade promotions with mechanics, depth, feature/display, and costs
  - post-promo dip (pantry loading) after each promotion
  - sell-in shipments with forward buying ahead of promotions
  - monthly shipment targets (for ship attainment)

All brands and retailers are fictional. No client data is used.

Run:  uv run cpg-mcp-generate
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from cpg_analytics_mcp.config import Settings

SEED = 42
VOLUME_SCALE = 5  # national-retailer weekly volumes
N_WEEKS = 104
LAST_WEEK_ENDING = dt.date(2026, 9, 26)  # a Saturday

# category -> (price elasticity, post-promo dip rate, seasonality fn(week_of_year))
CATEGORIES = {
    "Pain Relief": (2.2, 0.12, lambda w: 1 + 0.10 * np.cos(2 * np.pi * (w - 4) / 52)),
    "Cold & Flu": (1.8, 0.05, lambda w: 1 + 0.65 * np.cos(2 * np.pi * (w - 2) / 52)),
    "Oral Care": (2.6, 0.22, lambda w: np.ones_like(w, dtype=float)),
    "Hand Hygiene": (2.4, 0.18, lambda w: 1 + 0.20 * np.cos(2 * np.pi * (w - 6) / 52)),
    "Laundry": (3.0, 0.28, lambda w: 1 + 0.08 * np.cos(2 * np.pi * (w - 28) / 52)),
}

# brand -> (category, [(ppg, [(pack, list_price, base_units)])])
BRANDS = {
    "Neurex": ("Pain Relief", [
        ("Neurex Tablets", [("16 tabs", 2.40, 900), ("32 tabs", 4.10, 520)]),
        ("Neurex Gel", [("50 g", 5.20, 260), ("100 g", 8.60, 140)]),
    ]),
    "Breathe+": ("Cold & Flu", [
        ("Breathe+ Capsules", [("16 caps", 4.50, 480), ("32 caps", 7.20, 230)]),
        ("Breathe+ Hot Drink", [("5 sachets", 3.80, 400), ("10 sachets", 6.40, 210)]),
    ]),
    "DentaGuard": ("Oral Care", [
        ("DentaGuard Toothpaste", [("75 ml", 1.90, 1400), ("2x75 ml", 3.40, 600)]),
        ("DentaGuard Mouthwash", [("500 ml", 3.60, 520), ("1 L", 5.90, 240)]),
    ]),
    "PuraHand": ("Hand Hygiene", [
        ("PuraHand Liquid Soap", [("250 ml", 1.60, 1100), ("Refill 500 ml", 2.70, 650)]),
        ("PuraHand Sanitiser", [("50 ml", 1.50, 700), ("250 ml", 3.20, 380)]),
    ]),
    "FreshFold": ("Laundry", [
        ("FreshFold Liquid", [("1.3 L", 5.50, 650), ("2.6 L", 9.80, 300)]),
        ("FreshFold Pods", [("20 pods", 6.20, 560), ("40 pods", 11.50, 240)]),
    ]),
}

# brand -> (cannibalisation, halo) between its two PPGs, as a share of the promoted PPG's incremental
# sales value ("source of volume"): 0.32 = 32% of the extra value was taken from the sibling PPG.
# Substitutes (liquid vs pods) cannibalise each other; complements (toothpaste + mouthwash) get a halo.
CROSS_EFFECTS = {
    "Neurex": (0.20, 0.04),      # tablets vs gel: partial substitutes
    "Breathe+": (0.04, 0.08),    # capsules + hot drink: used together
    "DentaGuard": (0.03, 0.10),  # toothpaste + mouthwash: complements
    "PuraHand": (0.14, 0.04),    # liquid soap vs sanitiser: partial substitutes
    "FreshFold": (0.32, 0.02),   # liquid vs pods: strong substitutes
}
HALO_SUPPORT = {"feature": 0.5, "display": 0.8}  # in-store visibility multiplies the halo
MAX_CROSS_LOSS = 0.6  # a sibling never loses more than 60% of its baseline in a week

# retailer -> (channel, size factor, promos per PPG per year, fixed cost per promo GBP, max weeks,
#              promo response factor)
RETAILERS = {
    "MegaMart": ("Grocery", 1.00, 8, 6000, 3, 1.15),
    "ValueKing": ("Discounter", 0.70, 2, 1500, 2, 0.55),  # EDLP shoppers barely react to promos
    "QuickStop": ("Convenience", 0.25, 4, 1000, 2, 0.85),
    "HealthPlus Pharmacy": ("Pharmacy", 0.35, 6, 2000, 2, 1.00),
    "ShopNow Online": ("Online", 0.30, 6, 1800, 1, 0.95),
}

MECHANICS = {  # mechanic -> (discount range, uplift multiplier)
    "TPR": ((0.15, 0.33), 1.00),        # temporary price reduction
    "Multibuy": ((0.25, 0.34), 1.10),   # e.g. 2 for £X
    "BOGOF": ((0.50, 0.50), 0.90),      # buy one get one free; shoppers must buy two
}


def _weeks() -> pd.DataFrame:
    ends = [LAST_WEEK_ENDING - dt.timedelta(weeks=i) for i in range(N_WEEKS)][::-1]
    df = pd.DataFrame({"week_ending": pd.to_datetime(ends)})
    df["week_id"] = range(1, N_WEEKS + 1)
    df["fiscal_year"] = df.week_ending.dt.year
    df["fiscal_month"] = df.week_ending.dt.month
    df["fiscal_quarter"] = df.week_ending.dt.quarter
    df["month_start"] = df.week_ending.dt.to_period("M").dt.to_timestamp()
    df["week_of_year"] = df.week_ending.dt.isocalendar().week.astype(int)
    return df


def _products(rng: np.random.Generator) -> pd.DataFrame:
    rows, sku = [], 1
    for brand, (category, ppgs) in BRANDS.items():
        for ppg, packs in ppgs:
            for pack, price, base in packs:
                rows.append({
                    "sku_id": f"SKU{sku:03d}",
                    "sku_name": f"{ppg} {pack}",
                    "brand": brand,
                    "category": category,
                    "ppg": ppg,
                    "pack_size": pack,
                    "list_price": price,
                    "cogs_per_unit": round(price * rng.uniform(0.40, 0.55), 2),
                    "_base": base,
                })
                sku += 1
    return pd.DataFrame(rows)


def _retailers() -> pd.DataFrame:
    return pd.DataFrame([
        {"retailer_id": f"R{i + 1}", "retailer_name": n, "channel": c}
        for i, (n, (c, *_)) in enumerate(RETAILERS.items())
    ])


def _promotions(rng: np.random.Generator, products: pd.DataFrame, retailers: pd.DataFrame) -> pd.DataFrame:
    rows, pid = [], 1
    for _, r in retailers.iterrows():
        _, _, per_year, fixed, max_len, _ = RETAILERS[r.retailer_name]
        n_promos = int(per_year * N_WEEKS / 52)
        for ppg in products.ppg.unique():
            # non-overlapping slots, leaving >= 3 clear weeks after each promo for the dip
            starts, week = [], int(rng.integers(2, 8))
            gap = max(4, N_WEEKS // max(n_promos, 1))
            while week < N_WEEKS - 4 and len(starts) < n_promos:
                starts.append(week)
                week += gap + int(rng.integers(-1, 3))
            for s in starts:
                mech = rng.choice(list(MECHANICS), p=[0.6, 0.25, 0.15])
                (lo, hi), _ = MECHANICS[mech]
                length = int(rng.integers(1, max_len + 1))
                rows.append({
                    "promo_id": f"P{pid:04d}",
                    "retailer_id": r.retailer_id,
                    "ppg": ppg,
                    "mechanic": mech,
                    "discount_pct": round(float(rng.uniform(lo, hi)), 2),
                    # share of the discount the manufacturer funds; the retailer funds the rest
                    "funding_pct": round(float(rng.uniform(0.45, 0.75)), 2),
                    "feature": bool(rng.random() < 0.45),
                    "display": bool(rng.random() < 0.35),
                    "start_week_id": s,
                    "end_week_id": min(s + length - 1, N_WEEKS),
                    "fixed_cost": round(fixed * rng.uniform(0.7, 1.3), 0),
                })
                pid += 1
    return pd.DataFrame(rows)


def _promo_lift(pr, elasticity: float, response: float, n_weeks: int) -> np.ndarray:
    """Weekly lift (share of baseline) for a promotion on its own PPG."""
    # diminishing-returns response curve: deep cuts lift more, but not enough to pay
    lift = elasticity * pr.discount_pct * (1 + pr.discount_pct)
    lift = (lift * MECHANICS[pr.mechanic][1] + 0.45 * pr.feature + 0.60 * pr.display) * response
    return lift * np.linspace(1.0, 0.8, n_weeks)  # fatigue across a multi-week promo


def _baseline(p, size: float, weeks: pd.DataFrame) -> np.ndarray:
    """Expected weekly units for a SKU at a retailer with no promotion."""
    season = CATEGORIES[p.category][2]
    baseline = p._base * VOLUME_SCALE * size * season(weeks.week_of_year.to_numpy())
    return baseline * (1 + 0.0006 * weeks.week_id.to_numpy())  # mild trend


def _sales(rng, weeks, products, retailers, promos) -> pd.DataFrame:
    frames = []
    ppg_brand = products.drop_duplicates("ppg").set_index("ppg").brand
    for _, r in retailers.iterrows():
        size, response = RETAILERS[r.retailer_name][1], RETAILERS[r.retailer_name][5]
        rp = promos[promos.retailer_id == r.retailer_id]
        # baseline sales value per PPG at this retailer, to size cross-product effects
        ppg_value: dict[str, np.ndarray] = {}
        for _, p in products.iterrows():
            ppg_value[p.ppg] = ppg_value.get(p.ppg, 0) + _baseline(p, size, weeks) * p.list_price
        for _, p in products.iterrows():
            elasticity, dip_rate, _ = CATEGORIES[p.category]
            baseline = _baseline(p, size, weeks)
            units = baseline * rng.normal(1.0, 0.06, N_WEEKS)
            promo_col = np.array([None] * N_WEEKS, dtype=object)
            price = np.full(N_WEEKS, p.list_price)
            own_promo_or_dip = np.zeros(N_WEEKS, dtype=bool)

            for _, pr in rp[rp.ppg == p.ppg].iterrows():
                idx = np.arange(pr.start_week_id - 1, pr.end_week_id)
                incr = baseline[idx] * _promo_lift(pr, elasticity, response, len(idx))
                own_promo_or_dip[pr.start_week_id - 1:pr.end_week_id + 2] = True
                units[idx] += incr * rng.normal(1.0, 0.08, len(idx))
                promo_col[idx] = pr.promo_id
                price[idx] = p.list_price * (1 - pr.discount_pct)
                # post-promo dip (pantry loading): 60% / 40% over the next two weeks
                dip = incr.sum() * dip_rate
                for k, share in ((1, 0.6), (2, 0.4)):
                    j = pr.end_week_id - 1 + k
                    if j < N_WEEKS and promo_col[j] is None:
                        units[j] -= dip * share

            # cross-product effects: a promo on the brand's other PPG cannibalises or lifts this one.
            # Not applied while this PPG is on its own deal (no price gap to switch on) or in its
            # post-promo dip, which also keeps each promo's own economics unchanged.
            cannib, halo = CROSS_EFFECTS[p.brand]
            sibling_promos = rp[(rp.ppg != p.ppg) & (rp.ppg.map(ppg_brand) == p.brand)]
            for _, pr in sibling_promos.iterrows():
                idx = np.arange(pr.start_week_id - 1, pr.end_week_id)
                support = 1 + HALO_SUPPORT["feature"] * pr.feature + HALO_SUPPORT["display"] * pr.display
                # promoted PPG's incremental value x rate, spread over this PPG's SKUs by value share,
                # expressed as a share of this SKU's baseline
                value_ratio = ppg_value[pr.ppg][idx] / ppg_value[p.ppg][idx]
                effect = (halo * support - cannib) * _promo_lift(pr, elasticity, response, len(idx)) * value_ratio
                clean = ~own_promo_or_dip[idx]
                units[idx[clean]] += baseline[idx[clean]] * np.maximum(effect[clean], -MAX_CROSS_LOSS)

            units = np.maximum(units, 0).round()
            frames.append(pd.DataFrame({
                "week_ending": weeks.week_ending,
                "retailer_id": r.retailer_id,
                "sku_id": p.sku_id,
                "units": units.astype(int),
                "baseline_units": baseline.round(1),
                "avg_price": price.round(2),
                "sales_value": (units * price).round(2),
                "promo_id": promo_col,
            }))
    return pd.concat(frames, ignore_index=True)


def _shipments(rng, weeks, products, sales) -> pd.DataFrame:
    """Sell-in: retailer orders ~ next week's expected sales, plus forward buying."""
    out = []
    price = products.set_index("sku_id").list_price
    for (rid, sku), g in sales.groupby(["retailer_id", "sku_id"], sort=False):
        g = g.sort_values("week_ending")
        nxt = g.units.shift(-1).fillna(g.units.iloc[-1]).to_numpy(dtype=float)
        promo_next = g.promo_id.shift(-1).notna().to_numpy()
        ordered = nxt * rng.normal(1.0, 0.07, len(g))
        ordered[promo_next] *= 1.25  # forward buy ahead of promo
        fill = rng.uniform(0.90, 1.0, len(g))
        shipped = (ordered * fill).round()
        out.append(pd.DataFrame({
            "week_ending": g.week_ending.to_numpy(),
            "retailer_id": rid,
            "sku_id": sku,
            "ordered_units": ordered.round().astype(int),
            "shipped_units": shipped.astype(int),
            "shipped_value": (shipped * price[sku]).round(2),
        }))
    return pd.concat(out, ignore_index=True)


def _targets(rng, weeks, products, shipments) -> pd.DataFrame:
    s = shipments.merge(products[["sku_id", "brand"]], on="sku_id").merge(
        weeks[["week_ending", "month_start"]], on="week_ending")
    m = s.groupby(["month_start", "retailer_id", "brand"], as_index=False).shipped_value.sum()
    # targets set "a year ago": growth ambition +4% with planning noise
    m["target_value"] = (m.shipped_value * rng.normal(1.04, 0.06, len(m))).round(-2)
    return m.drop(columns="shipped_value")


VIEWS = """
CREATE OR REPLACE VIEW v_sales_enriched AS
SELECT s.week_ending, w.fiscal_year, w.fiscal_month, w.fiscal_quarter, w.month_start,
       r.retailer_id, r.retailer_name, r.channel,
       p.sku_id, p.sku_name, p.brand, p.category, p.ppg, p.pack_size,
       s.units, s.baseline_units, s.avg_price, s.sales_value,
       s.promo_id, (s.promo_id IS NOT NULL) AS is_promo,
       pr.mechanic, pr.discount_pct
FROM fact_sales s
JOIN dim_week w USING (week_ending)
JOIN dim_retailer r USING (retailer_id)
JOIN dim_product p USING (sku_id)
LEFT JOIN fact_promotions pr USING (promo_id);

CREATE OR REPLACE VIEW v_promo_performance AS
WITH promo_rows AS (
    SELECT pr.promo_id, s.sku_id, s.units, s.baseline_units,
           p.list_price, p.cogs_per_unit, pr.discount_pct, pr.funding_pct
    FROM fact_promotions pr
    JOIN fact_sales s ON s.promo_id = pr.promo_id
    JOIN dim_product p ON p.sku_id = s.sku_id
),
during AS (
    SELECT promo_id,
           SUM(units) AS promo_units,
           SUM(baseline_units) AS baseline_units,
           SUM((units - baseline_units) * (list_price - cogs_per_unit)) AS incr_margin_during,
           SUM(units * list_price * discount_pct * funding_pct) AS variable_trade_spend
    FROM promo_rows GROUP BY promo_id
),
dip AS (  -- two non-promoted weeks after the promo, same retailer x PPG
    SELECT pr.promo_id,
           SUM(s.units - s.baseline_units) AS dip_units,
           SUM((s.units - s.baseline_units) * (p.list_price - p.cogs_per_unit)) AS dip_margin
    FROM fact_promotions pr
    JOIN dim_week we ON we.week_id = pr.end_week_id
    JOIN dim_product p ON p.ppg = pr.ppg
    JOIN fact_sales s ON s.sku_id = p.sku_id AND s.retailer_id = pr.retailer_id
         AND s.week_ending > we.week_ending
         AND s.week_ending <= we.week_ending + INTERVAL 14 DAY
         AND s.promo_id IS NULL
    GROUP BY pr.promo_id
),
ppg_attr AS (
    SELECT ppg, ANY_VALUE(brand) AS brand, ANY_VALUE(category) AS category
    FROM dim_product GROUP BY ppg
),
cross_product AS (  -- same brand's other PPG, same retailer, during the promo weeks
    -- Measured from data: sibling actual vs baseline. Negative = cannibalisation, positive = halo.
    -- Skips sibling weeks contaminated by the sibling's own promo or its post-promo dip.
    SELECT pr.promo_id,
           SUM(s.units - s.baseline_units) AS cross_units,
           SUM((s.units - s.baseline_units) * (p.list_price - p.cogs_per_unit)) AS cross_margin
    FROM fact_promotions pr
    JOIN ppg_attr a ON a.ppg = pr.ppg
    JOIN dim_product p ON p.brand = a.brand AND p.ppg <> pr.ppg
    JOIN dim_week w ON w.week_id BETWEEN pr.start_week_id AND pr.end_week_id
    JOIN fact_sales s ON s.sku_id = p.sku_id AND s.retailer_id = pr.retailer_id
         AND s.week_ending = w.week_ending AND s.promo_id IS NULL
    WHERE NOT EXISTS (
        SELECT 1 FROM fact_promotions sp
        WHERE sp.retailer_id = pr.retailer_id AND sp.ppg = p.ppg
          AND w.week_id BETWEEN sp.end_week_id + 1 AND sp.end_week_id + 2)
    GROUP BY pr.promo_id
),
calc AS (
    SELECT d.promo_id, d.promo_units, d.baseline_units, d.variable_trade_spend,
           d.promo_units - d.baseline_units AS gross_incr,
           COALESCE(dp.dip_units, 0) AS dip_units,
           d.incr_margin_during + COALESCE(dp.dip_margin, 0) AS incr_margin,
           COALESCE(cp.cross_units, 0) AS cross_units,
           COALESCE(cp.cross_margin, 0) AS cross_margin,
           cp.promo_id IS NULL AS cross_unmeasured
    FROM during d
    LEFT JOIN dip dp USING (promo_id)
    LEFT JOIN cross_product cp USING (promo_id)
)
SELECT pr.promo_id, r.retailer_name, r.channel, pr.ppg, a.brand, a.category,
       pr.mechanic, pr.discount_pct, pr.funding_pct,
       CASE WHEN pr.discount_pct < 0.20 THEN '<20%'
            WHEN pr.discount_pct < 0.30 THEN '20-29%'
            WHEN pr.discount_pct < 0.45 THEN '30-44%' ELSE '45%+' END AS discount_band,
       pr.feature, pr.display,
       ws.week_ending AS start_week, we.week_ending AS end_week,
       pr.end_week_id - pr.start_week_id + 1 AS duration_weeks,
       c.promo_units,
       ROUND(c.baseline_units, 0) AS baseline_units,
       ROUND(c.gross_incr, 0) AS gross_incremental_units,
       ROUND(c.dip_units, 0) AS post_promo_dip_units,
       ROUND(c.gross_incr + c.dip_units, 0) AS net_incremental_units,
       ROUND(c.promo_units / NULLIF(c.baseline_units, 0) - 1, 3) AS uplift_pct,
       ROUND(c.incr_margin, 2) AS incremental_gross_margin,
       ROUND(c.variable_trade_spend, 2) AS variable_trade_spend,
       pr.fixed_cost,
       ROUND(c.variable_trade_spend + pr.fixed_cost, 2) AS total_trade_spend,
       ROUND(c.incr_margin / NULLIF(c.variable_trade_spend + pr.fixed_cost, 0), 3) AS gross_roi,
       ROUND((c.incr_margin - c.variable_trade_spend - pr.fixed_cost)
             / NULLIF(c.variable_trade_spend + pr.fixed_cost, 0), 3) AS net_roi,
       ROUND(c.cross_units, 0) AS cross_product_units,
       ROUND(c.cross_margin, 2) AS cross_product_margin,
       CASE WHEN c.cross_unmeasured THEN 'not measurable'  -- sibling on its own deal or in its dip
            WHEN c.cross_units < 0 THEN 'cannibalisation'
            WHEN c.cross_units > 0 THEN 'halo' ELSE 'none' END AS cross_effect,
       ROUND(c.incr_margin + c.cross_margin, 2) AS range_incremental_margin,
       ROUND((c.incr_margin + c.cross_margin)
             / NULLIF(c.variable_trade_spend + pr.fixed_cost, 0), 3) AS range_roi
FROM fact_promotions pr
JOIN calc c USING (promo_id)
JOIN dim_retailer r USING (retailer_id)
JOIN ppg_attr a ON a.ppg = pr.ppg
JOIN dim_week ws ON ws.week_id = pr.start_week_id
JOIN dim_week we ON we.week_id = pr.end_week_id;

CREATE OR REPLACE VIEW v_ship_attainment AS
WITH ship AS (
    SELECT w.month_start, sh.retailer_id, p.brand,
           SUM(sh.shipped_value) AS shipped_value,
           SUM(sh.shipped_units) AS shipped_units,
           SUM(sh.ordered_units) AS ordered_units
    FROM fact_shipments sh
    JOIN dim_week w USING (week_ending)
    JOIN dim_product p USING (sku_id)
    GROUP BY ALL
)
SELECT s.month_start, YEAR(s.month_start) AS fiscal_year, MONTH(s.month_start) AS fiscal_month,
       r.retailer_name, r.channel, s.brand,
       ROUND(s.shipped_value, 2) AS shipped_value, t.target_value,
       ROUND(s.shipped_value / NULLIF(t.target_value, 0), 3) AS ship_attainment_pct,
       s.shipped_units, s.ordered_units,
       ROUND(s.shipped_units / NULLIF(s.ordered_units, 0), 3) AS case_fill_rate
FROM ship s
JOIN fact_targets t USING (month_start, retailer_id, brand)
JOIN dim_retailer r USING (retailer_id);
"""


def build(db_path: Path) -> dict[str, int]:
    rng = np.random.default_rng(SEED)
    weeks = _weeks()
    products = _products(rng)
    retailers = _retailers()
    promos = _promotions(rng, products, retailers)
    sales = _sales(rng, weeks, products, retailers, promos)
    shipments = _shipments(rng, weeks, products, sales)
    targets = _targets(rng, weeks, products, shipments)

    tables = {
        "dim_week": weeks.drop(columns="week_of_year"),
        "dim_product": products.drop(columns="_base"),
        "dim_retailer": retailers,
        "fact_promotions": promos,
        "fact_sales": sales,
        "fact_shipments": shipments,
        "fact_targets": targets,
    }
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    con = duckdb.connect(str(db_path))
    for name, df in tables.items():
        con.register("_df", df)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _df")
        con.unregister("_df")
    con.execute(VIEWS)
    con.close()
    return {k: len(v) for k, v in tables.items()}


def main() -> None:
    settings = Settings.from_env()
    counts = build(settings.db_path)
    print(f"Built {settings.db_path}")
    for k, v in counts.items():
        print(f"  {k:<16} {v:>7,} rows")


if __name__ == "__main__":
    main()
