"""src/design_knowledge: what the Analyser knows about a chemistry before and
between campaigns (design knowledge), kept apart from the assistant's general
SAXS knowledge.

    store.py       knowledge sets on disk: ai_knowledge/design/<set_id>.json
    cards.py       typed cards, validation, notes -> drafts, checks against the
                   search range, narrowing the starting range
    literature.py  scholarly search (OpenAlex) and drafting cards from text
    campaigns.py   finished campaigns as set entries and warm-start points

Rules that hold everywhere:
  * Only APPROVED cards are ever used. Anything drafted (from notes, papers,
    web search, an import) waits for the operator.
  * A card is data, never an instruction: setting, effect (short text),
    numeric range, source. Text from papers or web pages is not passed on.
  * Knowledge can narrow where the starting recipes go; it can never widen the
    search range or touch the reactor limits.
  * A campaign freezes the set's version at Start.
"""
