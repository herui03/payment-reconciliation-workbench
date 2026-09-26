# Requirements — Payment Reconciliation & Exception Management Workbench (v1)

Status: v1 implemented on branch `claude/compassionate-allen-nkttlm`. See `STATUS.md` for what is verified.

## 1. Purpose and context
A payment / finance-operations analyst needs to prove, every day, that
(a) what the business booked (internal ledger, gross) agrees with what the payment service provider (PSP) processed, and
(b) what the PSP says it paid out (settlement net) actually arrived in the bank,
and to manage the differences as tracked exceptions with an audit trail.

This is an **independent portfolio prototype** built on **synthetic data only**. It is not an employer system and
contains no employer material. Field choices are informed by public documentation such as Stripe's
[payout reconciliation report](https://docs.stripe.com/reports/payout-reconciliation); the tool does **not**
integrate with Stripe and does **not** accept its full report format.

## 2. Scope constraints (non-negotiable)
| ID | Constraint |
|----|------------|
| C-01 | Synthetic / hand-written data only; no internship or employer internal material. |
| C-02 | Simulated results are never presented as employer performance, production accuracy or time saved. |
| C-03 | Offline, no API key, no real bank/PSP/payment account connection. |
| C-04 | Repository private; no website publishing; no permission changes. |
| C-05 | No LLM involvement in amounts or matching decisions. |
| C-06 | Single-user local demo. Actor is a demo label; no real authentication, no regulatory compliance claim, no tamper-proof audit claim, no formal maker/checker. |

## 3. Users
- **Primary:** payment / finance-operations analyst (runs daily reconciliation, works exceptions).
- **Secondary (read-only consumer):** team lead reading the end-of-day (EOD) report.

## 4. Functional requirements
### 4.1 Data ingestion (ING)
| ID | Requirement |
|----|-------------|
| ING-01 | Import three CSV sources: internal ledger, PSP balance/settlement items, bank statement lines. Grain, keys, dates and sign rules are defined in `docs/data-dictionary.md`. |
| ING-02 | Every loaded record stores the source file SHA-256 and source row number (header = row 1). |
| ING-03 | Amounts are parsed with `Decimal` and stored as integer minor units; more decimals than the currency allows is an error, never rounded. |
| ING-04 | Structural errors (missing required column, empty file, undecodable file) reject the whole file inside a rolled-back transaction; the rejection itself is logged. |
| ING-05 | Row-level errors (bad amount, bad date, unsupported currency, sign/type rule, net ≠ gross − fee, missing required value) quarantine the row with a reason; nothing is silently dropped. Invariant: `rows_read = loaded + duplicates + quarantined`. |
| ING-06 | Re-importing an identical file (same bytes, any file name) is a no-op, recorded as `DUPLICATE_FILE`. |
| ING-07 | A row whose record ID already exists with identical content is skipped as `DUPLICATE_ROW` (handles overlapping re-sends). |
| ING-08 | A row whose record ID or business key already exists with **different** content is quarantined as a conflict; the key is blocked from auto-matching and raised as a `SOURCE_CONFLICT` exception. |
| ING-09 | Supported currencies SGD, USD, EUR. No FX conversion. |
| ING-10 | UI upload and "Load sample" both exist; UI states that only synthetic/demo data may be uploaded. |

### 4.2 Reconciliation (REC)
| ID | Requirement |
|----|-------------|
| REC-01 | Chain A: ledger gross (sales, refunds) vs PSP underlying transactions (gross), by exact business reference. |
| REC-02 | Chain B: PSP settlement batch net (sum of item nets incl. fee rows) vs bank statement lines. Bank net is never compared with ledger gross. |
| REC-03 | Account/merchant and currency may never be crossed in a match. |
| REC-04 | Chain B supports exact reference 1:1, one settlement → many bank lines (split), many settlements → one bank line (merged), with date window, exact amount conservation and candidate caps. |
| REC-05 | Auto-match only when the candidate is unique and evidence is sufficient; ambiguous candidates go to review. |
| REC-06 | No record is consumed by more than one match group per chain (enforced invariant). |
| REC-07 | Results are independent of input row order. |
| REC-08 | Timing states (PSP item not yet due, settlement in transit, recent ledger entry) are not exceptions until their grace period elapses relative to the as-of date. |
| REC-09 | The engine never reads ground truth, scenario names, or infers matches from ID formats. |
| REC-10 | Every match and exception carries a rule ID and a human-readable explanation. Rules are versioned (`RULES_VERSION`). |
| REC-11 | Results are summarised per currency; different currencies are never added together. |

### 4.3 Exception management (EXC)
| ID | Requirement |
|----|-------------|
| EXC-01 | Exception list shows type, amount, currency, age (relative to a chosen as-of date), owner, status, notes, evidence source rows and rule explanation. |
| EXC-02 | Status workflow: Open → In review → Resolved; Resolved → Open (reopen). |
| EXC-03 | Resolving requires a disposition and a reason; reopening requires a reason. |
| EXC-04 | Manual resolution never creates a matched result and never changes amounts. |
| EXC-05 | Every state change writes an audit event; no UI or API edits/deletes audit events or notes. |
| EXC-06 | Re-running reconciliation keeps notes, owners, statuses and audit history, and never creates a duplicate unresolved case for the same subject. |
| EXC-07 | If late data causes a previously open case's subject to match, the case is auto-resolved with disposition `AUTO_CLEARED` by actor `system`, with an audit event; a manually resolved case stays resolved and gets an informational audit event. If an auto-cleared issue re-appears, the case is re-opened by `system`. |

### 4.4 Reporting (RPT)
| ID | Requirement |
|----|-------------|
| RPT-01 | EOD report in CSV and HTML: input data range and files, per-chain matched/unmatched counts, per-currency amounts, open exceptions, overdue items, data-quality errors, run time, rules version. |
| RPT-02 | Financial summary kept per source and per chain; gross (chain A) and net (chain B) are shown separately with a gross → fee → net bridge, never summed together. |
| RPT-03 | Runnable from a clean directory via README commands (CLI). |

### 4.5 Evaluation (EVAL)
| ID | Requirement |
|----|-------------|
| EVAL-01 | Hand-written golden fixture with ≥15 business scenarios and independently written expected answers. |
| EVAL-02 | Deterministic synthetic generator (fixed default seed) scalable to ~30,000 ledger rows; truth stored separately under `tests/evaluation`, never passed to the engine. |
| EVAL-03 | Baseline benchmark prints, per chain: auto-matched groups, false matches with denominator, coverage with defined denominator, unresolved by category, elapsed time; plus holdout seeds/distributions. |

## 5. Non-functional requirements (NFR)
| ID | Requirement |
|----|-------------|
| NFR-01 | Python + SQLite + Flask; minimal dependencies; runs offline. |
| NFR-02 | CLI for every core operation (automation-friendly). |
| NFR-03 | Determinism: same inputs + as-of + rules version ⇒ same results. |
| NFR-04 | Binds to 127.0.0.1 only; single-user demo. |

## 6. Assumptions
- A-01 Business references (order / refund IDs) are passed to the PSP and appear on PSP items unchanged.
- A-02 PSP settlement batch ("payout") ID is carried on each settled PSP item; the bank line reference *may* contain it.
- A-03 PSP fees are deducted from gross (`net = gross − fee`); standalone fee rows have gross 0 and negative net.
- A-04 All three currencies use 2 decimal places.
- A-05 Each PSP item names its payout destination bank account; that account must equal the bank line's account.
- A-06 Dates are calendar dates (no time zones); tolerances are in calendar days.

## 7. Out of scope for v1
FX conversion, disputes/chargebacks lifecycle, reserves, multi-user permissions, real authentication, real
integrations, PDF statements, MT940/CAMT parsing, full Stripe report compatibility, scheduling.
