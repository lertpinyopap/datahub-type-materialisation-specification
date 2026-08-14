# Agent Instructions

This repository defines the Type Materialisation Specification and its Python
implementation. Treat the specification as the product: documentation changes
can alter downstream implementation behavior.

## Working Principles

- Keep edits tight, small, and directly related to the user's request.
- Confirm with the user before changing the direction, scope, or semantics of
  any major component of the specification.
- Prefer additive clarification over broad rewrites unless the user explicitly
  asks for restructuring.
- Preserve terminology once introduced. If a term needs to change, update all
  affected documentation in the same edit.
- Mark unsettled design choices as open questions rather than silently deciding
  them.
- Keep examples valid YAML and aligned with the formal specification.
- Put a short label immediately before every YAML sample so readers can tell it
  is an example, not normative grammar.
- When adding an enum to the grammar, describe each enum value in `SPEC.md`.

## Specification Intent

The specification describes how raw, flat, or schema-on-read data is
materialised into typed relational data. It should cover:

- Source format declaration.
- Control data for materialisation and error handling.
- Target data declaration.
- Field mapping.
- Data typing.
- Nullability and uniqueness.
- Validation rules.
- Failure handling.
- Inheritance or extension between reusable specifications.

## Implementation Guidance

The implementation should target dbt as the materialisation runtime. Python may
be used for parsing, validation, generation, and test tooling. When adding
implementation notes:

- Keep them subordinate to the specification.
- Do not introduce Python as a separate materialisation runtime unless
  explicitly agreed.
- Prefer simple implementation behavior over production-specific assumptions.
- Add conformance-oriented examples where useful.

## Python Implementation

The Python implementation in `src/type_materialisation/` is product
implementation code, not miscellaneous utility code. The installed CLI command
is `tms`.

- Target Python 3.13.
- Local development uses the repository-root `requirements.txt`, which stays on
  the dbt 1.12 Snowflake adapter line until an upgrade is intentionally planned.
- Use idiomatic, typed Python with standard-library facilities where practical.
- Keep dependencies explicit in `requirements.txt`; install package wiring with
  `python -m pip install --no-build-isolation --no-deps -e .` after installing
  requirements.
- Keep package code under `src/type_materialisation/`.
- Keep the public CLI command named `tms`, even though the import package is
  `type_materialisation`.
- Prefer small modules with clear responsibility: parsing, schema validation,
  CSV validation, macro loading, and dbt generation support.
- Custom macros are Python objects implementing the Type Materialisation macro
  interface. SQL generation is required; local Python execution is optional and
  should produce a warning in `tms validate` when absent.
- Do not add generated dbt project files to the repository unless explicitly
  requested; generated dbt artifacts should normally remain transient.

## Change Control

Before making a change, identify whether it is:

- Editorial: improves wording without changing meaning.
- Clarifying: makes existing intent more precise.
- Semantic: changes the behavior expected from an implementation.

Semantic changes require explicit user confirmation before editing.
