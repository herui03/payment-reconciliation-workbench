# SOP — daily reconciliation (demo workbench)

Scope: synthetic/demo data on a local machine. Single user; the actor field is a label, not a login.

## Daily steps
1. **Set actor label** (top right) to your demo name.
2. **Import** the day's ledger, PSP and bank CSVs (Dashboard → Upload CSV, choose the source).
   - `LOADED`: all rows in. `PARTIAL`: some rows quarantined → open *Data quality*, fix at source, re-send only
     the corrected rows (already-loaded identical rows are skipped automatically).
   - `REJECTED`: nothing loaded (structure problem, or strict mode). Fix the file and re-import.
   - `DUPLICATE_FILE`: this exact content was already imported — nothing to do.
   - Use *Strict* when a file must load all-or-nothing.
3. **Reconcile** with today's as-of date. Do not reconcile with an earlier as-of to "tidy up": earlier as-of runs
   are read-only snapshots and never change cases.
4. **Review Chain A then Chain B** on the dashboard, per currency. Never add currencies together; never compare
   bank amounts with ledger gross.
5. **Work exceptions** (Exceptions page; filter by type/currency; ⚑ overdue = older than 3 days):
   | Type | First check |
   |------|-------------|
   | AMOUNT_MISMATCH (A) | PSP gross vs order system; partial capture? |
   | MISSING_IN_PSP / MISSING_IN_LEDGER | Was the payment failed/voided? Booking missed? |
   | DATE_OUT_OF_TOLERANCE | Confirm same event; late capture |
   | ACCOUNT / CURRENCY_MISMATCH | Wrong merchant or currency booked — correction at source |
   | SOURCE_CONFLICT | Two versions of one record — ask the source owner which is right; re-send corrected file |
   | PARTIAL_RECEIPT / MISSING_IN_BANK | Payout trace from PSP; watch next statement |
   | AMBIGUOUS_CANDIDATES / SUGGESTED_GROUP | Use bank/PSP remittance detail to confirm; resolve with `MANUAL_MATCH_CONFIRMED` + evidence in reason |
   | UNEXPECTED_BANK_CREDIT | Non-PSP credit (interest, refund from supplier)? |
   | UNSETTLED_OVERDUE | PSP hold/reserve? Raise with PSP |
   Assign an owner, add a note for every contact made, move to *In review*.
6. **Resolve** only with a disposition and a reason ≥10 characters. Resolving does not make the item "matched";
   it will show as "still unmatched" until the data reconciles. Reopen (with reason) if new facts arrive.
7. **Generate the EOD report** (Reports). Check: overdue list, data-quality section, per-currency totals.
8. Next day: import new files and reconcile — cases whose subject now matches are auto-cleared by `system`
   with the rule cited; your notes stay.

## Controls and limits
- Audit log is read-only in the app; this is not tamper-proof storage and not a regulatory record.
- No maker/checker: one person can resolve their own case in this demo.
- Report for an older run = snapshot of that run (title says so); only the latest run shows live case state.
