# Design knowledge sets (Analyser only)

One JSON file per chemistry, written by the Autonomous Analyser's Knowledge tab
(`src/design_knowledge/store.py`): approved, draft and retired cards, the
finished campaigns that used the set (with their measured runs), a version and a
change log. In git, so a set travels with the code; use Export / Import to share
one by file (imported cards arrive as drafts).

This is knowledge for choosing the next recipe. The assistant (Guinier) does not
read it, and nothing here is indexed. `SWAXS_DESIGN_KNOWLEDGE_DIR` overrides the
folder.
