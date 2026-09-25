"""Mock SNR must be high enough that a good in-band fit clears the campaign's
confidence gate, so the closed loop can converge in mock mode.

Background: convergence requires confidence ≥ confidence_min (0.5). With the old
flux (1e6) the subtracted curve was so noisy at high q that a visually good fit
scored ~0.14 confidence, so the loop never converged and ran the whole budget.
Raising the simulator flux lifts the SNR so a normal acquisition (10×10 s) scores
well above 0.5. This guards that relationship (and the campaign gate value).
"""
import numpy as np

from src.analysis.nanoparticle import analyze_profile
from src.simulator.pattern import iq_curve, background_curve
from src.optimizer.campaign import CampaignController
from src.optimizer.space import ParameterSpace


FLUX = 2.0e7          # matches reactor/config.yml spec.simulator.flux


def _mock_subtracted(flux, exposure=10.0, frames=10, R=5.0, pdi=0.137, seed=0):
    rng = np.random.default_rng(seed)
    q = np.linspace(0.03, 3.0, 260)
    bkg = background_curve(q, solvent_bkg=50.0, capillary=120.0)
    Is = iq_curve(q, R, pdi, scale=20000.0, bkg=bkg, porod=0.0)

    def noisy(I):
        c = I * exposure * flux / 1e6
        acc = np.zeros_like(c)
        for _ in range(frames):
            acc += rng.poisson(np.clip(c, 0, 5e7))
        return acc / frames

    s, b = noisy(Is), noisy(bkg)
    sub = np.clip(s - b, 1e-6, None)
    sig = np.sqrt(np.clip(s + b, 1, None)) / frames
    return q, sub, sig


def test_good_mock_fit_clears_the_confidence_gate():
    confs = []
    for seed in range(6):
        q, I, sig = _mock_subtracted(FLUX, seed=seed)
        confs.append(analyze_profile(q, I, sig, dist="auto")["confidence"])
    # every normal-acquisition fit must clear the 0.5 convergence gate
    assert min(confs) >= 0.5, f"mock confidence below the gate: {confs}"


def test_low_flux_would_not_clear_the_gate():
    # documents WHY the flux was raised: at the old 1e6 the same fit fails the gate
    q, I, sig = _mock_subtracted(1.0e6, seed=0)
    assert analyze_profile(q, I, sig, dist="auto")["confidence"] < 0.5


def test_campaign_gate_is_still_half():
    # if this default changes, revisit the flux ↔ confidence budget above
    space = ParameterSpace.from_config({})
    c = CampaignController(space, target_size=5.0, tolerance=0.3, pdi_cap=0.15)
    assert c.confidence_min == 0.5
