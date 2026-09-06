"""
tests/test_retrieval_eval.py — retrieval-quality eval (OPT-IN, ADVERSARIAL)
===========================================================================
A measured baseline for the RAG collections the assistant STILL semantically
searches: ``literature``, ``user_papers`` and ``beamline``. (The ``apps``
collection is no longer retrieved — its knowledge.md is injected directly into
the system prompt; see src/ai/assistant.py::_resolve_app_knowledge.)

Why ADVERSARIAL
---------------
The first version of this eval used a well-separated corpus and saturated at
recall@1 = 1.000 — with no headroom it could only ever detect a CATASTROPHIC
regression, never an improvement or a subtle one. This version is built to be
hard, so there is something to measure:
  • near-duplicate content ACROSS collections (a literature method doc and a
    user_paper that both lean on the same quantity — Rg/I0, Porod slope, d-spacing);
  • WRONG-KEYWORD questions whose obvious search terms appear most prominently in
    a DISTRACTOR doc, not the intended source;
  • PARAPHRASED questions that share little/no vocabulary with their source, so a
    hit depends on the embedding's semantics, not lexical overlap.
The measured baseline sits well below 1.0 (see the assertion), so a retrieval
change that helps OR hurts will move the number.

Safety
------
- Runs ONLY against a fresh KnowledgeBase rooted at a pytest ``tmp_path`` — it
  never opens, reads, or writes the live ``ai_knowledge/vector_db``.
- OPT-IN: it loads the sentence-transformers embedding model (slow), so it is
  SKIPPED unless ``SWAXS_RUN_RETRIEVAL_EVAL=1`` is set. Enable with::

      SWAXS_RUN_RETRIEVAL_EVAL=1 pytest tests/test_retrieval_eval.py -s

Version portability
-------------------
The fixture KB contains ONLY the three RAG collections, so ``retrieve(query)``
(default = all collections) is identical to the assistant's production call
``retrieve(query, collections=RAG_COLLECTIONS)`` on this fixture.
"""

from __future__ import annotations

import os
import pytest

# ── opt-in gate (keep the normal suite fast; never load the model implicitly) ──
_OPT_IN = os.environ.get("SWAXS_RUN_RETRIEVAL_EVAL", "").strip().lower() in (
    "1", "true", "yes", "on",
)
pytestmark = pytest.mark.skipif(
    not _OPT_IN,
    reason="retrieval eval is opt-in (loads the embedding model); "
           "set SWAXS_RUN_RETRIEVAL_EVAL=1 to run it",
)


# ── Adversarial fixture corpus ─────────────────────────────────────────────────
# Deliberately overlapping: each doc shares dominant vocabulary with at least one
# doc in ANOTHER collection, so a lexical/naive retriever confuses them.
_CORPUS: list[tuple[str, str, str]] = [
    # ---- literature: general method physics ----------------------------------
    ("literature", "guinier_analysis.md", """
    Guinier analysis. At very low q the intensity follows I(q) = I0 exp(-q^2
    Rg^2/3). The radius of gyration Rg comes from the slope of ln I(q) versus
    q^2, and the forward scattering I0 (the zero-angle intensity) from the
    intercept. The approximation holds while q*Rg is below about 1.3 for a
    globular particle. Upward curvature at the lowest q indicates aggregation;
    downward curvature indicates interparticle repulsion.
    """),
    ("literature", "porod_analysis.md", """
    Porod law. For a two-phase system with a sharp smooth interface the intensity
    decays as q^-4 at high q, so the log-log slope is -4. A slope between -3 and
    -4 indicates a rough or fractal surface. The Porod invariant, the integral of
    q^2 I(q), together with the Porod constant gives the specific surface area
    per unit volume. These are general relations, independent of any one sample.
    """),
    ("literature", "kratky_analysis.md", """
    The Kratky plot, q^2 I(q) versus q, and its dimensionless form (q Rg)^2
    I(q)/I0 versus q Rg, report compactness and flexibility. A compact folded
    globular particle gives a bell-shaped peak near q Rg = sqrt(3) with height
    about 1.104; an unfolded, extended or disordered chain gives a rising plateau
    that does not come back down.
    """),
    ("literature", "pair_distance_analysis.md", """
    The pair-distance distribution function p(r) is recovered from the scattering
    by an indirect Fourier transform (GNOM). It is the distribution of all
    intra-particle distances and falls to zero at the maximum dimension Dmax. Its
    shape classifies overall geometry and gives a real-space cross-check on Rg.
    """),
    ("literature", "form_factor_models.md", """
    Form factors. The sphere model has sharp minima setting the radius. The
    cylinder shows a q^-1 rod regime at low q from the length and a q^-4 decay
    from the radius. The lamellar form factor gives a q^-2 decay and Bragg orders
    at q* = 2*pi/d, where d is the repeat spacing. Core-shell models add contrast
    between an inner core and an outer shell.
    """),

    # ---- user_papers: sample-specific, overlapping the method docs above -----
    ("user_papers", "albumin_reference_study.md", """
    Albumin reference measurement. The radius of gyration Rg comes from the slope
    of ln I(q) versus q^2, and the forward scattering I0 (the zero-angle
    intensity) from the intercept. For this bovine serum albumin standard the
    molar mass is obtained from the zero-angle intensity divided by the mass
    concentration, using the known contrast and partial specific volume. A
    dilution series is measured to extrapolate to infinite dilution. The monomer
    radius of gyration for this sample is about 2.7 nm and the mass about 66 kDa.
    """),
    ("user_papers", "polyamide_membrane_study.md", """
    Polyamide thin-film composite membrane. For a sharp smooth interface the
    intensity decays as q^-4 and the log-log slope is -4; a slope between -3 and
    -4 indicates a rough or fractal surface. Grazing-incidence measurements of
    this reverse-osmosis selective layer characterise the crumpled ridge-and-
    valley surface roughness and the buried void network. For THIS film the
    high-q power-law slope reports the fractal roughness of the polymer/air
    interface, and a broad feature reports the characteristic void spacing;
    cross-link density is inferred from the void size distribution.
    """),
    ("user_papers", "mrna_lipid_particle_study.md", """
    mRNA lipid nanoparticle. Bragg orders appear at q* = 2*pi/d, where d is the
    repeat spacing. This delivery particle shows an internal correlation peak
    whose position q* gives the repeat spacing d = 2*pi/q* of the ionizable lipid
    / nucleic-acid mesophase, a few nanometres. The spacing and whether the
    arrangement is lamellar or inverse-hexagonal shift with the N/P ratio and the
    lipid composition; loss of the peak means cargo release.
    """),
    ("user_papers", "block_copolymer_micelle_study.md", """
    Amphiphilic block copolymer micelle. This assembly is modelled as a dense
    hydrophobic core surrounded by a solvated PEG corona. The aggregation number
    follows from the core volume and the corona thickness from the shell
    contrast; above the critical micelle concentration an inter-micelle
    correlation appears. Temperature and salt tune the aggregation number.
    """),

    # ---- beamline: two docs that overlap on i0 / bstop -----------------------
    ("beamline", "ssrl_bl15_endstation.md", """
    SSRL beamline 1-5 endstation. The default photon energy is 12 keV. Data
    acquisition is orchestrated by the SPEC control program through its bServer
    HTTP interface, with motor and shutter states read over EPICS. The incident
    flux is monitored by an upstream ion chamber (i0) and the transmitted beam by
    a photodiode on the beamstop. A flow cell and a temperature stage are
    available.
    """),
    ("beamline", "flux_normalization_modes.md", """
    Flux normalization. The incident flux is monitored by an upstream ion chamber
    (i0) and the transmitted beam by a photodiode on the beamstop. Each frame is
    divided by one scalar before integration. In bstop mode the factor gives
    transmission-corrected intensity I = counts/(i0*T). In i0 mode only the
    incident flux is used. In absolute mode a calibration constant K from a
    glassy-carbon or water standard converts to the differential cross-section in
    cm^-1. Combining terms is rejected as a physics error, and any frame whose
    corrected i0 or beamstop reading is non-positive is skipped.
    """),
]

# ── Fixed eval questions: (question, expected_source_name, kind) ────────────────
#   kind is documentation only: 'paraphrase' (no shared vocab), 'wrong_keyword'
#   (dominant terms live in a distractor), 'direct' (fair).
_QUESTIONS: list[tuple[str, str, str]] = [
    # paraphrase — no 'Guinier'/'Rg'/'q'
    ("How do I read a particle's overall size from the way the curve levels off "
     "at the very smallest scattering angles?", "guinier_analysis.md", "paraphrase"),
    # wrong-keyword — 'zero-angle intensity' is loudest in guinier, answer is albumin
    ("How do I get the molar mass of my protein reference from the zero-angle "
     "intensity and its concentration?", "albumin_reference_study.md", "wrong_keyword"),
    # direct-ish but competes with the membrane paper on 'slope/fractal'
    ("For a particle with a perfectly smooth sharp boundary, what high-q power-law "
     "exponent governs the tail?", "porod_analysis.md", "direct"),
    # wrong-keyword — 'fractal/slope/surface' are in porod_analysis too
    ("In the reverse-osmosis polyamide film, what does the high-q slope reveal "
     "about the interface?", "polyamide_membrane_study.md", "wrong_keyword"),
    # paraphrase — no 'Kratky'
    ("Which plot tells me whether my protein is folded and compact versus "
     "unfolded and floppy?", "kratky_analysis.md", "paraphrase"),
    # form factor vs LNP both carry 'd = 2 pi / q*'
    ("Which scattering model produces Bragg orders at q* = 2*pi/d?",
     "form_factor_models.md", "wrong_keyword"),
    # LNP, but 'spacing / d=2pi/q*' also in form_factor_models
    ("What internal repeat spacing does the correlation peak of the mRNA delivery "
     "particle report?", "mrna_lipid_particle_study.md", "direct"),
    # micelle vs form-factor core-shell
    ("For the PEG-corona block copolymer assembly, which structural model "
     "applies?", "block_copolymer_micelle_study.md", "wrong_keyword"),
    # paraphrase — no 'p(r)'/'Dmax'
    ("How do I obtain the largest internal distance and a real-space size profile "
     "from the scattering data?", "pair_distance_analysis.md", "paraphrase"),
    # beamline energy/control — competes with normalization doc on i0/bstop
    ("What photon energy and control server does the SSRL 1-5 endstation use?",
     "ssrl_bl15_endstation.md", "direct"),
    # wrong-keyword — 'beamstop diode' also in the endstation doc
    ("When the transmitted-beam diode reads non-positive, what happens to that "
     "frame during flux normalization?", "flux_normalization_modes.md", "wrong_keyword"),
    # guinier aggregation vs albumin dilution
    ("What curvature at the lowest angles signals that my sample is aggregating?",
     "guinier_analysis.md", "direct"),
    ("How is the total scattering invariant related to the specific surface "
     "area?", "porod_analysis.md", "direct"),
    # paraphrase — no 'Porod'
    ("How is the crumpled roughness of the selective polymer layer characterised "
     "by scattering?", "polyamide_membrane_study.md", "paraphrase"),
    ("For a rigid rod, which low-q power-law regime appears and how is the length "
     "inferred?", "form_factor_models.md", "direct"),
    ("How does the N/P ratio change the internal mesophase of the nucleic-acid "
     "lipid particle?", "mrna_lipid_particle_study.md", "direct"),
    ("What does absolute-scale normalization use to convert counts to a "
     "differential cross-section?", "flux_normalization_modes.md", "direct"),
    # wrong-keyword — 'radius of gyration' dominant in guinier_analysis
    ("What monomer radius of gyration is expected for the albumin calibration "
     "standard?", "albumin_reference_study.md", "wrong_keyword"),
    ("Where does the dimensionless compactness plot peak for an ideal globular "
     "particle?", "kratky_analysis.md", "direct"),
    # wrong-keyword — both beamline docs mention i0
    ("Which detector monitors the incident flux upstream of the sample?",
     "ssrl_bl15_endstation.md", "wrong_keyword"),
    # near-duplicate contests: the signature sentence now lives in a paper too,
    # and the GENERAL method doc is the intended source — several will lose to
    # the near-duplicate, which is the point (headroom to measure).
    ("How is the radius of gyration obtained from the slope of ln I(q) versus "
     "q^2?", "guinier_analysis.md", "near_duplicate"),
    ("For a sharp smooth interface, why is the high-q log-log slope equal to -4?",
     "porod_analysis.md", "near_duplicate"),
    ("At what q value do Bragg orders appear for a lamellar repeat spacing d?",
     "form_factor_models.md", "near_duplicate"),
    ("Which upstream detector reading is used as i0?",
     "ssrl_bl15_endstation.md", "near_duplicate"),
    ("What slope range at high q indicates a rough or fractal surface?",
     "porod_analysis.md", "near_duplicate"),
    ("What is the zero-angle intensity and how is it read from the fit?",
     "guinier_analysis.md", "near_duplicate"),
    ("During flux correction, which reading represents the transmitted beam?",
     "ssrl_bl15_endstation.md", "wrong_keyword"),
]

_KS = (1, 3, 5)


@pytest.fixture(scope="module")
def eval_kb(tmp_path_factory):
    """Throwaway KnowledgeBase in a temp dir. NEVER touches the live vector_db."""
    pytest.importorskip("chromadb")
    pytest.importorskip("sentence_transformers")
    from src.ai.knowledge import KnowledgeBase

    base = tmp_path_factory.mktemp("retrieval_eval_kb")
    kb = KnowledgeBase(base)
    for collection, name, text in _CORPUS:
        n = kb.ingest_text(text, name=name, collection=collection)
        assert n >= 1, f"fixture ingest produced no chunks for {name}"
    return kb


def _topk_sources(kb, question: str, k: int) -> list[str]:
    """Distinct source names of the top-k retrieved chunks, best first. On a
    fixture holding only the RAG collections, retrieve(query) == the production
    retrieve(query, collections=RAG_COLLECTIONS)."""
    hits = kb.retrieve(question, top_k=k)
    seen: list[str] = []
    for h in hits:
        s = h.get("source")
        if s and s not in seen:
            seen.append(s)
    return seen[:k]


def test_retrieval_recall_at_k(eval_kb):
    """Report recall@1/3/5 over the adversarial question set and list every miss.

    Asserts a floor on recall@1 (the metric with the most headroom on this hard
    corpus) set just below the measured baseline, so a real regression fails here
    while normal embedding-model jitter does not."""
    per_q = []
    for q, expected, kind in _QUESTIONS:
        top = _topk_sources(eval_kb, q, max(_KS))
        per_q.append((q, expected, kind, top))

    total = len(_QUESTIONS)
    recall = {k: 0 for k in _KS}
    misses = {k: [] for k in _KS}
    for q, expected, kind, top in per_q:
        for k in _KS:
            if expected in top[:k]:
                recall[k] += 1
            else:
                got = top[0] if top else "(none)"
                misses[k].append(f"    [{kind}] want {expected!r}, got {got!r}: {q}")

    print("\n" + "=" * 72)
    print(f"ADVERSARIAL RETRIEVAL EVAL — {total} questions "
          "(literature/user_papers/beamline)")
    print("=" * 72)
    for k in _KS:
        print(f"  recall@{k}: {recall[k]}/{total} = {recall[k] / total:.3f}")
    for k in (1,):                          # the misses that matter for headroom
        if misses[k]:
            print(f"\n  MISSES @{k}:")
            print("\n".join(misses[k]))
    print("=" * 72)

    # Measured baseline (all-MiniLM-L6-v2): recall@1 = 21/27 = 0.778, recall@3 =
    # recall@5 = 1.000 (the intended doc is always within the top 3; only top-1
    # has error, which is the headroom). Floor set just under 0.778 with ~2
    # questions of margin for a future embedding-model swap; a real regression
    # (which drops many) fails here, unlike the old saturated 1.000 corpus.
    floor1 = 0.70
    assert recall[1] / total >= floor1, (
        f"recall@1 = {recall[1] / total:.3f} < floor {floor1}; retrieval "
        "regressed. Misses@1:\n" + "\n".join(misses[1]))
