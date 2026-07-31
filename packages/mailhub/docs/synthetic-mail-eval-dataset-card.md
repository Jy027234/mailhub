# Synthetic MailHub intelligence evaluation dataset card

## Purpose

`evals/synthetic_mail_eval.jsonl` is a small, deterministic contract set for
the evidence-first analyzer. It checks project/task/date extraction, quoted
history removal, HTML/prompt-injection handling, decision/risk/commitment
evidence, knowledge-candidate gating, and low-information abstention.

## Data provenance and rights

- All text is authored synthetic text created for this repository.
- No production mailbox, personal data, credential, attachment, provider
  response, or customer fixture is included.
- Addresses and identifiers are fictional; the cases contain no real contact
  information.
- The dataset is released with the MailHub module license/NOTICE boundary and
  must not be replaced by copied mailbox exports.

## Format and evaluation contract

Each JSONL row has `case_id`, `label`, `subject`, `body`, and `expected` fields.
The test constructs a governed `MailMessageProjection`, runs the deterministic
`rules-v1` analyzer, and asserts the expected safety/extraction invariants.
`scripts/evaluate_synthetic.py` emits a versioned JSON regression report and is
run by the package verification gate; its default dataset path is this file's
repository sibling and it never accepts production mailbox content.
Cases are intentionally small and are not a statistical benchmark. Any model-
backed evaluation must use a separately versioned, redacted or synthetic set,
record parser/model/policy versions, and keep production content out of
training and fixtures.

## Known limitations

The set does not measure multilingual recall, OCR/attachment parsing, provider
specific MIME quirks, calibration, latency, or real-world prevalence. Passing
this contract is necessary but not sufficient for a model, canary, or release.
