"""
src/reactor/ — Pump-control layer for the continuous-flow nanoparticle reactor.

Based on Fong et al., J. Chem. Phys. 154, 224201 (2021): 5 Mitos P-pumps with
Dolomite LG16 flow sensors.  This package is the EXECUTION layer only — it
receives an already-predicted recipe (from an external BO/SAXS step) and drives
the pumps.  It does not implement the optimization.

Public API:
  load_config         — read reactor/config.yml
  Recipe, RecipeError — recipe validation + fraction→flow conversion
  PumpBank            — mock/real pump backend (swappable)
  ReactorController   — run/flush state machine
"""

from .config import load_config
from .recipe import Recipe, RecipeError, recipe_to_setpoints
from .hardware import PumpBank, MockPump
from .controller import ReactorController, STATES

__all__ = [
    "load_config",
    "Recipe", "RecipeError", "recipe_to_setpoints",
    "PumpBank", "MockPump",
    "ReactorController", "STATES",
]
