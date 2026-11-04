# Deduplicate and aggregate orders

`data/orders.csv` has columns `order_id,customer,amount,status,updated_at`. The export is messy: some rows are exact
duplicates, and some orders appear several times with different `updated_at` (ISO-8601, lexicographically sortable)
because they were revised. Produce `summary.json` in the workspace root:

1. Keep, for each `order_id`, only the row with the latest `updated_at`.
2. Ignore orders whose (latest) `status` is `cancelled`.
3. Output a JSON object mapping each remaining customer to `{"orders": <count>, "total": <sum of amount, rounded
   to 2 decimals>}`. Customers with no remaining orders must not appear.

Any JSON formatting is fine. You may put a helper script under `bin/`.
