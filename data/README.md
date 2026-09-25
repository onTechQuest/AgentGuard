# Deterministic evaluation fixtures

`orders.json` is local business test data, not a customer database. Existing
ORD-1001 (shipped), ORD-1002 (processing), and ORD-1003 (delivered) are unchanged.

Milestone 12B adds only two orders to exercise the live tool's return boundary:

| Order | Delivery date | Age on 2026-09-10 | Expected eligibility | Purpose |
| --- | --- | ---: | --- | --- |
| ORD-1030 | 2026-08-11 | 30 days | true | Verify the last included day of the return window. |
| ORD-1031 | 2026-08-10 | 31 days | false | Verify the first day after the window expires. |

The `CUST-RETURN-30/31` customer IDs and `BOUNDARY30/31` tracking values identify
these synthetic boundary fixtures. Their totals and carrier are incidental test
data, not prerequisites for semantic correctness. Dates deliberately use the
existing fixed `RETURN_EVALUATION_DATE = 2026-09-10`, never the system clock.
No production tool logic or agent instructions changed.
