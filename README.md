# Explanation Service with an Audit Trail

Person 1 submission for the Big Data Systems and Responsible AI mini project. This repository contains the storage design, Iceberg schema proposal, and synthetic example records.

## Architecture

```mermaid
flowchart LR
    A[Synthetic loan application] --> B[Versioned feature transform]
    B --> C[Versioned model and explainer]
    C --> D[Prediction event]
    D --> E[(Iceberg prediction_events)]
    D --> F[(Iceberg input_snapshots)]
    D --> G[(Iceberg explanation_records)]
    D --> H[(Iceberg model_registry)]
    E --> I[Historical lookup by prediction ID]
    F --> I
    G --> I
    H --> I
    E -. Parquet + ZSTD .-> J[Object storage and Iceberg snapshot metadata]
```

## Person 1 deliverables

- [Storage design report (DOCX)](person1_storage_design.docx)
- [Storage design (Markdown)](person1_storage_design.md)
- [Iceberg table DDL proposal](iceberg_schema.sql)
- [Synthetic example records](synthetic_audit_records.json)

## Design summary

- Four logical Iceberg tables: `prediction_events`, `input_snapshots`, `explanation_records`, and `model_registry`.
- Parquet data files with Zstandard compression; time-based partitions on decision time for events and created time for linked artifacts; monthly partitions are an option for small demonstrations.
- Immutable event and artifact records linked by generated UUIDs, with SHA-256 hashes for canonical inputs and explanations.
- Retain Iceberg snapshots and immutable model, preprocessing, explainer, and policy artifacts to support point-in-time replay.
- Do not partition by applicant or include direct borrower identifiers. Use only synthetic records in this project implementation.

## Data and privacy note

The supplied Lending Club CSV contains borrower-level financial, employment, location, and free-text fields. The course brief prohibits real personal data, so this repo does not contain that CSV or any real borrower values. The included examples are synthetic. `Default` in the source data is a final repayment outcome, not a historical accept/deny decision.

## Status and setup

This is Person 1's architecture submission. The SQL is a proposed Spark SQL / Iceberg schema and has not been applied to a running lakehouse. Person 2 can use it as the storage baseline when implementing the query interface and reproducibility test. To review, open the DOCX or Markdown report; inspect the table definitions in `iceberg_schema.sql`; use `synthetic_audit_records.json` as a contract fixture. No dataset download is required.

## Dataset provenance

Suggested source record: [Lending Club loan dataset for granting models, Zenodo 11295916](https://zenodo.org/records/11295916). Confirm the applicable license from an authoritative record before redistribution. The project's privacy requirement takes precedence; synthetic input is used here.
