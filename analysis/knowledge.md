# Analysis App — Knowledge Base

## Purpose
The Analysis app (port 5004) provides structural analysis tools for
background-subtracted 1D scattering curves.  It implements Guinier analysis,
Kratky plots, Porod analysis, and basic pair-distance estimation.

## Guinier Analysis

### Theory
At very low q (qRg < 1.3), the scattering intensity follows:

```
I(q) ≈ I(0) · exp(−q²Rg²/3)
```

Linearised (Guinier plot): **ln I(q) vs q²** gives a straight line with:
- Slope = −Rg²/3  →  Rg = sqrt(−3 · slope)
- Intercept = ln I(0)  →  I(0) = exp(intercept)

### Validity Criterion
The Guinier approximation is valid for **qRg ∈ [0.3, 1.3]**.
- qRg < 0.3: risk of beamstop artefact, beam divergence effects
- qRg > 1.3: higher-order terms become significant, Rg underestimated

### Rg Interpretation
| Rg (nm) | Approximate particle          |
|---------|-------------------------------|
| 1–2     | small globular protein (20 kDa)|
| 2–4     | medium protein (50–150 kDa)   |
| 4–8     | large protein / small complex  |
| > 10    | large complex, nanoparticle    |

### Common Errors
- **Upturn in Guinier plot**: aggregation or repulsion (check low q)
- **Curvature downward**: polydispersity, multiple species
- **χ² > 2**: poor fit — adjust range or discard frame

### Auto-range Selection
The app suggests a Guinier range by:
1. Starting from q_min (first point above beamstop shadow, q ≥ 0.005 nm⁻¹)
2. Estimating initial Rg from a broad fit
3. Refining: q_min ≥ 0.3/Rg_est, q_max ≤ 1.3/Rg_est
4. Iterating once with the refined Rg

## Kratky Analysis

### Standard Kratky
Plot: **q²·I(q) vs q**

- Folded globular protein: bell-shaped peak, returns to zero
- Partially unfolded: broad, elevated at high q
- Fully unstructured (IDP): monotonically increasing plateau

### Dimensionless Kratky
Plot: **(qRg)²·I(q)/I(0) vs qRg**

Universal reference point for ideal globule: **(√3, 3/e) ≈ (1.732, 1.103)**
- Peak above reference: flatter than ideal globule (possible unfolding)
- Peak at reference, sharp descent: compact, folded
- No peak: highly flexible / disordered

## Porod Analysis

### Theory
At high q, for a particle with a sharp surface:

```
I(q) → K_p / q⁴   (Porod law)
```

Porod plot: **q⁴·I(q) vs q⁴** — should plateau to a constant K_p (Porod constant).

Deviations:
- Oscillating plateau: monodisperse particles (sphere, cylinder form factor)
- Continuously rising: diffuse interface (polymer, unfolded protein)
- Not reaching plateau: need higher q data (WAXS may be needed)

### Porod Invariant Q*
```
Q* = ∫₀^∞ I(q) · q² dq ≈ 2π² · Δρ² · φ · (1−φ)
```
Used to determine contrast and volume fraction.

## Pair Distance Distribution p(r)

### Theory
The p(r) function is the Fourier transform of I(q):

```
p(r) = (r / 2π²) ∫₀^∞ I(q) · q · sin(qr) dq
```

It represents the probability of finding two scattering centres at distance r.

### Key Parameters
- **Dmax**: maximum dimension of particle — p(r) = 0 for r > Dmax
- Shape of p(r) reveals particle shape:
  - Globular: bell-shaped, symmetric
  - Elongated: asymmetric, long tail to high r
  - Hollow: two-peak distribution

### Notes
- Dmax must be set by user — it affects the entire p(r) shape
- Rule of thumb: Rg ≈ 0.77 × (Dmax/2) for spherical particles
- Software: GNOM (ATSAS), BIFT, or BayesIFT

## Output and Manifest Registration
Analysis results are saved in `manifest.json` under the top-level `analyses`
section, keyed by a generated uuid (one entry per analysis run):
```json
"analyses": {
  "f4c2…uuid": {
    "id": "f4c2…uuid",
    "type": "guinier",
    "file_path": "/path/to/subtracted.dat",
    "params": { "q_min": 0.012, "q_max": 0.045 },
    "results": { "Rg": 3.14, "I0": 0.0142, "chi2": 0.98 },
    "fit_range": [0.012, 0.045],
    "quality_score": 0.92,
    "ai_assessment": "Rg = 3.14 nm, qRg range valid …",
    "provenance": { "app": "analysis", "run_id": "…" },
    "created_at": "2026-01-15T12:00:00Z"
  }
}
```

## src/ Imports
- `src.manifest` — register analysis results
- `src.utils.read_dat_metadata.read_dat_data_metadata` — load .dat files
