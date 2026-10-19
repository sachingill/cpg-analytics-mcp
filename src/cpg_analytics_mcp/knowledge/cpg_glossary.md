# CPG Commercial Analytics Glossary

## The two sales numbers
- **Sell-out**: what consumers buy at the till (retailer EPOS / scan data). Drives *demand* analysis. → `fact_sales`
- **Sell-in**: what the manufacturer ships to the retailer (invoiced shipments). Drives *revenue recognition and targets*. → `fact_shipments`
- They diverge because retailers hold stock. Before a promotion, retailers **forward buy** (sell-in spikes before sell-out does).

## Product hierarchy
Category → Brand → **PPG** (Promoted Product Group: SKUs promoted together, e.g. all sizes of a toothpaste) → **SKU**.
Promotions are planned at PPG × retailer level.

## Baseline and uplift
- **Baseline**: the volume that would have sold *without* the promotion. It is always a model estimate, never observed.
- **Gross incremental units** = promo-week units − baseline units.
- **Uplift %** = promo units ÷ baseline − 1. 100% means volume doubled.
- **Post-promo dip**: consumers stocked up (pantry loading), so the weeks after a promo sell *below* baseline. Strongest in storable categories (laundry, oral care); weak in need-driven ones (cold & flu).
- **Net incremental units** = gross incremental + dip (the dip is negative).
- Not modelled here yet: **cannibalisation** (stealing from your own other SKUs) and **halo** (lifting related SKUs).

## Promotion mechanics
- **TPR**: temporary price reduction (e.g. 25% off).
- **Multibuy**: e.g. "2 for £5"; effective discount ~25–34%.
- **BOGOF**: buy one get one free = 50% effective discount.
- **Feature**: in the retailer's leaflet/app. **Display**: off-shelf placement (gondola end). Both lift volume independent of price.

## Trade spend
- **Variable trade spend** = promo units × list price × discount × funding share. It is paid on *every* promoted unit, including the baseline units that would have sold anyway. This is why deep discounts are expensive.
- **Funding share** (`funding_pct`): the retailer often co-funds the discount from its own margin; the manufacturer pays only its share. Negotiating this split is a major lever on ROI.
- **Fixed cost**: feature/display fees paid to the retailer.
- **Total trade spend** = variable + fixed.

## Promo ROI
- **Incremental gross margin** = net incremental units × (list price − COGS).
- **Gross ROI** = incremental gross margin ÷ total trade spend. ≥ 1.0 means the promotion paid back.
- **Net ROI** = (incremental gross margin − trade spend) ÷ trade spend = gross ROI − 1.
- Companies define ROI differently (gross vs net, margin vs revenue, with/without dip). Always state which one you use. This server's `promo_roi` metric is **gross ROI, net of post-promo dip**.
- Typical industry finding: a large share of promotions do not pay back.

## Cannibalisation and halo
- **Cannibalisation**: a promotion pulls sales from the manufacturer's *own* non-promoted products (e.g. liquid detergent on deal takes buyers from pods). That volume is moved, not gained, often from a higher-margin item.
- **Halo**: a promotion lifts sales of related non-promoted products (e.g. toothpaste on display lifts the same brand's mouthwash). Feature and display create more halo than price alone.
- **Measurement**: the brand's other PPG at the same retailer, actual vs baseline during the promo weeks. Weeks where that PPG is on its own deal or in its post-promo dip are excluded; if none remain, the effect is *not measurable*.
- **Range ROI** (`range_roi`) = (incremental gross margin + cross-product margin) ÷ total trade spend. A promo can pay back on its own PPG (`promo_roi` ≥ 1) and still lose money for the range.

## Ship attainment and service
- **Ship attainment %** = shipped value ÷ target value for the period. 1.0 = on target.
- **Case fill rate** = shipped units ÷ ordered units. The service level the retailer experienced.
- Low attainment with high fill rate = a demand problem. Low attainment with low fill rate = a supply problem.

## Channels
- **Grocery**: high promo intensity (hi-lo pricing).
- **Discounter**: EDLP (every day low price); rarely promotes, and promotions there tend to be low-ROI.
- **Convenience**, **Pharmacy**, **Online**: smaller, with different shopper missions.

## Calendar
Weeks end on Saturday. A week belongs to the month its week-ending date falls in.
