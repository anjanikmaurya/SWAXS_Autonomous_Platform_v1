"""
tests/test_retrieval_eval.py — minimal retrieval-quality eval (OPT-IN)
=====================================================================
A measured baseline for the RAG collections the assistant STILL semantically
searches: ``literature``, ``user_papers`` and ``beamline``. (The ``apps``
collection is no longer retrieved — its knowledge.md is injected directly into
the system prompt; see src/ai/assistant.py::_resolve_app_knowledge — so it is
deliberately NOT part of this eval.)

Why this exists
---------------
The chunker fix earlier in this project was validated by chunk lengths *looking*
healthier, not by measured retrieval quality. Without a number we cannot tell
whether a retrieval change (Change 1, or any future one) helped, hurt, or did
nothing. This test fixes ~18 questions, each tagged with the ONE source document
that should answer it, ingests a small controlled corpus into a TEMP KB, and
reports recall@k plus exactly which questions miss.

Safety
------
- Runs ONLY against a fresh KnowledgeBase rooted at a pytest ``tmp_path`` — it
  never opens, reads, or writes the live ``ai_knowledge/vector_db``. (That DB is
  currently empty from a blocked rebuild; a stray write would make things worse.)
- OPT-IN: it loads the sentence-transformers embedding model (slow, may download
  weights), so it is SKIPPED unless ``SWAXS_RUN_RETRIEVAL_EVAL=1`` is set. This
  keeps the normal suite fast. Enable with::

      SWAXS_RUN_RETRIEVAL_EVAL=1 pytest tests/test_retrieval_eval.py -s

Version portability
-------------------
The fixture KB contains ONLY the three RAG collections, so ``retrieve(query)``
(default = all collections) is identical to the assistant's production call
``retrieve(query, collections=RAG_COLLECTIONS)`` on this fixture. That lets the
exact same file run against pre- and post-Change-1 code for a clean comparison.
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


# ── Controlled fixture corpus ─────────────────────────────────────────────────
# Each entry: (collection, source_name, text). Topics are deliberately distinct
# so that a well-behaved retriever maps each question to exactly one source.
# Content is paraphrased domain knowledge — no copyrighted text.
_CORPUS: list[tuple[str, str, str]] = [
    # ---- literature: SAXS/WAXS method physics --------------------------------
    ("literature", "guinier_analysis.md", """
    Guinier analysis. At very low q the scattering intensity follows the Guinier
    approximation I(q) = I0 exp(-q^2 Rg^2 / 3), where Rg is the radius of
    gyration and I0 is the forward scattering. Plotting ln I(q) against q^2
    yields a straight line whose slope is -Rg^2/3. The fit is valid only in the
    Guinier region, conventionally q*Rg < 1.3 for globular particles and lower,
    near q*Rg < 1.0, for elongated rod-like particles. A lower bound q_min*Rg
    around 0.3 avoids beamstop and beam-divergence artefacts. Upward curvature at
    low q indicates aggregation; downward curvature suggests interparticle
    repulsion or a structure factor.
    """),
    ("literature", "porod_analysis.md", """
    Porod law and the Porod region. At high q, for a particle with a sharp,
    smooth interface, the intensity decays as I(q) ~ q^-4. On a log-log plot the
    high-q slope is therefore -4 for smooth surfaces; slopes between -3 and -4
    indicate rough or fractal surfaces, and a slope of -2 indicates Gaussian
    polymer chains or thin sheets. The Porod invariant Q, the integral of
    q^2 I(q), relates to the total scattering volume and the specific surface
    area per unit volume. A Porod constant is extracted from the plateau of
    q^4 I(q) versus q^4.
    """),
    ("literature", "kratky_plot.md", """
    The Kratky plot displays q^2 I(q) versus q and is a sensitive probe of
    particle compactness and flexibility. A compact, globular, well-folded
    particle produces a clear bell-shaped peak that returns to the baseline at
    high q. An unfolded, extended, or intrinsically disordered chain produces a
    monotonic plateau or upturn that does not return to zero. The dimensionless
    Kratky plot, (q Rg)^2 I(q)/I0 versus q Rg, normalizes for size and places the
    peak of an ideal compact sphere near q Rg = sqrt(3) with a peak height of
    about 1.1.
    """),
    ("literature", "pair_distance_pr.md", """
    The pair-distance distribution function p(r) is obtained from the scattering
    curve by an indirect Fourier transform (IFT), as implemented in GNOM. p(r)
    is the histogram of all intra-particle distances and goes to zero at the
    maximum particle dimension Dmax. The shape of p(r) reveals overall geometry:
    a symmetric bell indicates a globular particle, a skewed tail indicates an
    elongated particle, and multiple peaks indicate a multidomain or hollow
    structure. Both Rg and I0 can be recovered as moments of p(r), often more
    robustly than from a direct Guinier fit.
    """),
    ("literature", "form_factors.md", """
    Form factors describe the scattering from a single particle of a given shape.
    The sphere form factor has characteristic minima whose positions set the
    radius R, with R approximately Rg times sqrt(5/3). The cylinder form factor
    shows a q^-1 rod regime at low q from the length and a q^-4 decay from the
    radius at high q. The lamellar form factor produces a q^-2 decay and Bragg
    orders at q* = 2 pi / d, where d is the lamellar repeat spacing. Core-shell
    models add contrast between an inner core and an outer shell.
    """),
    ("literature", "radiation_damage.md", """
    Radiation damage in solution SAXS manifests as a progressive, dose-dependent
    change across successive exposures of the same sample: a rising low-q
    intensity from radiation-induced aggregation, or a falling I0 from
    fragmentation. Mitigations include flowing the sample through the beam,
    reducing exposure time, adding radical scavengers such as glycerol or
    ascorbate, and lowering the flux with attenuators. Frames are compared
    pairwise and damaged frames are discarded before averaging.
    """),

    # ---- user_papers: sample-specific system knowledge -----------------------
    ("user_papers", "lipid_nanoparticle_lnp.pdf", """
    Lipid nanoparticles (LNPs) for mRNA delivery show a SAXS signature dominated
    by an internal inverse-hexagonal or lamellar arrangement of the ionizable
    lipid and the encapsulated nucleic acid. A correlation peak at q* reports the
    internal repeat spacing d = 2 pi / q*, typically a few nanometres, which
    shifts with N/P ratio and lipid composition. The overall particle size is
    read from the low-q Guinier region. Loss of the internal peak indicates
    cargo release or structural collapse.
    """),
    ("user_papers", "bsa_standard.pdf", """
    Bovine serum albumin (BSA) is a common molecular-weight and absolute-scale
    calibration standard for biological SAXS. At infinite dilution monomeric BSA
    has a radius of gyration near 2.7 nm and a molecular weight of about 66 kDa.
    The forward scattering I0 on an absolute scale, divided by concentration,
    gives the molecular weight via the contrast and partial specific volume. A
    concentration series is measured to extrapolate out the structure factor.
    """),
    ("user_papers", "block_copolymer_micelle.pdf", """
    Amphiphilic block copolymer micelles are well described by a core-shell
    spherical form factor: a dense hydrophobic core and a solvated corona of
    hydrophilic blocks such as PEG. The aggregation number follows from the core
    volume, and the corona thickness follows from the shell contrast. Above the
    critical micelle concentration a structure factor peak appears from
    inter-micelle correlations. Temperature and salt tune the core-shell contrast
    and the aggregation number.
    """),
    ("user_papers", "membrane_tfc.pdf", """
    Thin-film composite (TFC) polyamide membranes for reverse osmosis are studied
    by grazing-incidence SAXS to characterize the crumpled ridge-and-valley
    surface roughness and the internal void structure of the polyamide selective
    layer. The high-q power-law slope reports the fractal roughness of the
    interface, and a broad correlation feature reports the characteristic void
    spacing. Cross-linking density is inferred from changes in the void size
    distribution.
    """),

    # ---- beamline: facility / instrument configuration -----------------------
    ("beamline", "ssrl_bl15_config.yml", """
    SSRL beamline 1-5 (BL 1-5) small/wide-angle scattering endstation. The
    default X-ray energy is 12 keV. Beam conditioning uses slits and a set of
    attenuator foils. Sample environment supports a flow cell and a temperature
    stage. Data acquisition is orchestrated through the SPEC control program via
    its bServer HTTP interface, and motor and shutter states are read over EPICS.
    The incident and transmitted flux are monitored by an upstream ion chamber
    (i0) and a photodiode on the beamstop.
    """),
    ("beamline", "detector_geometry.yml", """
    Detector geometry and calibration. The SAXS detector is a large-area module
    of shape 1043 by 981 pixels and the WAXS detector is 195 by 487 pixels. The
    sample-to-detector distance, beam centre, and detector tilt are stored in a
    pyFAI .poni calibration file generated from a silver behenate (AgBeh)
    standard. A mask file in EDF format flags the beamstop shadow, dead pixels,
    and module gaps so they are excluded from azimuthal integration.
    """),
    ("beamline", "normalization_notes.yml", """
    Normalization at the beamline. Each frame is normalized by a single scalar
    before integration. In bstop mode the factor is the beamstop-diode reading,
    giving transmission-corrected intensity I = counts/(i0*T). In i0 mode only the
    incident flux is used. In absolute mode a calibration constant K from a water
    or glassy-carbon standard converts to the differential cross-section in cm^-1.
    Combining normalization terms is a physics error and is rejected. Frames with
    a non-positive corrected i0 or bstop are skipped.
    """),
]

# ── Fixed eval questions: (question, expected_source_name) ────────────────────
_QUESTIONS: list[tuple[str, str]] = [
    # literature
    ("How do I determine the radius of gyration from the low-q slope of ln I versus q squared?",
     "guinier_analysis.md"),
    ("What is the valid q*Rg range for a Guinier fit on a rod-like particle?",
     "guinier_analysis.md"),
    ("Why does the high-q intensity decay as q to the minus four for a smooth interface?",
     "porod_analysis.md"),
    ("How do I get the specific surface area from the Porod invariant?",
     "porod_analysis.md"),
    ("What does a bell-shaped peak that returns to baseline tell me about protein folding?",
     "kratky_plot.md"),
    ("Where does the peak of a dimensionless Kratky plot sit for a compact sphere?",
     "kratky_plot.md"),
    ("How is the pair-distance distribution p(r) computed and what does Dmax mean?",
     "pair_distance_pr.md"),
    ("Which form factor gives a q^-1 rod regime at low q and how do I read the length?",
     "form_factors.md"),
    ("How do I recognise radiation damage across successive exposures and mitigate it?",
     "radiation_damage.md"),
    # user_papers
    ("What SAXS correlation peak reports the internal spacing of an mRNA lipid nanoparticle?",
     "lipid_nanoparticle_lnp.pdf"),
    ("How do I get the molecular weight of BSA from the forward scattering on an absolute scale?",
     "bsa_standard.pdf"),
    ("Which model fits an amphiphilic PEG block copolymer micelle with a core and corona?",
     "block_copolymer_micelle.pdf"),
    ("How is grazing-incidence SAXS used to study polyamide reverse-osmosis membrane roughness?",
     "membrane_tfc.pdf"),
    # beamline
    ("What X-ray energy and control software does SSRL beamline 1-5 use?",
     "ssrl_bl15_config.yml"),
    ("What are the SAXS and WAXS detector pixel dimensions and how is the .poni made?",
     "detector_geometry.yml"),
    ("What does bstop normalization mode compute and when are frames skipped?",
     "normalization_notes.yml"),
    ("Why is combining bstop and absolute normalization terms rejected as a physics error?",
     "normalization_notes.yml"),
    ("Which calibration standard is used to determine the sample-to-detector distance?",
     "detector_geometry.yml"),
]

# k values reported. recall@k = fraction of questions whose expected source is
# among the top-k retrieved sources.
_KS = (1, 3, 5)


@pytest.fixture(scope="module")
def eval_kb(tmp_path_factory):
    """Build a throwaway KnowledgeBase rooted in a temp dir and ingest the
    controlled corpus. NEVER touches the live ai_knowledge/vector_db."""
    pytest.importorskip("chromadb")
    pytest.importorskip("sentence_transformers")
    from src.ai.knowledge import KnowledgeBase

    base = tmp_path_factory.mktemp("retrieval_eval_kb")
    kb = KnowledgeBase(base)                     # db lives at base/vector_db
    for collection, name, text in _CORPUS:
        n = kb.ingest_text(text, name=name, collection=collection)
        assert n >= 1, f"fixture ingest produced no chunks for {name}"
    return kb


def _topk_sources(kb, question: str, k: int) -> list[str]:
    """The distinct source names of the top-k retrieved chunks, best first.

    Note: on a fixture holding only the RAG collections, retrieve(query) (all
    collections) is identical to the assistant's production call
    retrieve(query, collections=RAG_COLLECTIONS) — so this measures the real
    RAG path in a version-portable way."""
    hits = kb.retrieve(question, top_k=k)
    seen: list[str] = []
    for h in hits:
        s = h.get("source")
        if s and s not in seen:
            seen.append(s)
    return seen[:k]


def test_retrieval_recall_at_k(eval_kb):
    """Report recall@1/3/5 over the fixed question set and list every miss.

    Asserts a conservative floor on recall@5 so this doubles as a regression
    guard: a future retrieval change that drops recall@5 below the floor fails
    here. The floor is intentionally well below the measured baseline so normal
    embedding-model jitter does not flake it."""
    # Retrieve once at the largest k; recall@k for smaller k reuses the prefix.
    per_q: list[tuple[str, str, list[str]]] = []
    for q, expected in _QUESTIONS:
        top = _topk_sources(eval_kb, q, max(_KS))
        per_q.append((q, expected, top))

    total = len(_QUESTIONS)
    recall: dict[int, int] = {k: 0 for k in _KS}
    misses: dict[int, list[str]] = {k: [] for k in _KS}
    for q, expected, top in per_q:
        for k in _KS:
            if expected in top[:k]:
                recall[k] += 1
            else:
                misses[k].append(f"    - {expected!r}: {q}")

    # ── report (visible with `pytest -s`) ────────────────────────────────────
    print("\n" + "=" * 72)
    print(f"RETRIEVAL EVAL — {total} questions over literature/user_papers/beamline")
    print("=" * 72)
    for k in _KS:
        print(f"  recall@{k}: {recall[k]}/{total} = {recall[k] / total:.3f}")
    for k in _KS:
        if misses[k]:
            print(f"\n  MISSES @{k} (expected source not in top-{k}):")
            print("\n".join(misses[k]))
    print("=" * 72)

    # Conservative regression floor — measured baseline sits comfortably above.
    floor = 0.66
    assert recall[5] / total >= floor, (
        f"recall@5 = {recall[5] / total:.3f} < floor {floor}; retrieval "
        f"regressed. Misses@5:\n" + "\n".join(misses[5])
    )
