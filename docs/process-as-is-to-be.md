# As-is → To-be process

The "as-is" below is a **generic, assumed** manual process for a small payments operations team, written for
this portfolio project. It is not a description of any specific employer.

## As-is (assumed manual spreadsheet process)
```mermaid
flowchart TD
    A[Download ledger export] --> D[Paste into master spreadsheet]
    B[Download PSP payout report] --> D
    C[Download bank statement] --> D
    D --> E{VLOOKUP on order ID / amount}
    E -->|found| F[Mark green]
    E -->|not found| G[Highlight row yellow]
    G --> H[Email / chat PSP or bank]
    H --> I[Overwrite cell with comment]
    I --> J[Tomorrow: copy sheet, repeat]
    F --> J
```
Pain points (assumed): the same file pasted twice double-counts; amount lookups silently pick the first of two
equal amounts; gross and net get mixed in one total; currencies are summed; comments are overwritten; no record
of who decided what; late bank lines are not linked back to yesterday's open item.

## To-be (this workbench)
```mermaid
flowchart TD
    subgraph Ingest
      L[Ledger CSV] --> V[Validate + hash + dedupe]
      P[PSP CSV] --> V
      K[Bank CSV] --> V
      V -->|bad file| R[REJECTED, logged]
      V -->|bad rows| Q[Quarantine with reason]
      V -->|ok rows| S[(SQLite, append-only)]
    end
    S --> RUN[Reconcile as-of date]
    RUN --> CA[Chain A: ledger gross vs PSP gross]
    RUN --> CB[Chain B: settlement net vs bank]
    CA --> M[Match groups + rule explanation]
    CB --> M
    CA --> X[Exceptions -> cases]
    CB --> X
    X --> W[Analyst: assign, note, In review, Resolve with disposition + reason]
    W --> AU[(Audit events)]
    RUN --> AU
    M --> EOD[EOD report HTML + CSV]
    X --> EOD
    Q --> EOD
    NEW[Late data arrives] --> V
    V --> RUN2[Re-run]
    RUN2 -->|subject now matched| AC[Case AUTO_CLEARED by system, notes kept]
```

## Daily sequence (to-be)
```mermaid
sequenceDiagram
    participant A as Analyst
    participant W as Workbench
    A->>W: Import ledger / PSP / bank CSVs
    W-->>A: LOADED / PARTIAL / REJECTED / DUPLICATE_FILE with row counts + control totals
    A->>W: Reconcile (as-of)
    W-->>A: Chain A + Chain B per currency, exceptions, overdue
    A->>W: Work cases (owner, notes, status, disposition)
    A->>W: Generate EOD report
    Note over W: Next day: late bank line imported, re-run auto-clears matched cases
```
