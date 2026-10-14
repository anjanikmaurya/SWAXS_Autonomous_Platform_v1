# Assistant exam

Questions with expert answers and marking points, used to score the assistant
after any change to its knowledge or harness. Split into `practice` and
`heldout`; only practice questions may be used while tuning. To be filled.
Planned format, one JSON line per question:
`{id, split, app_id, question, must_mention: [...], must_not_say: [...], must_call_tools: [...], reference_answer}`.
