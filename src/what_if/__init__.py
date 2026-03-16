"""Counterfactual what-if projection utilities."""

from .projector import DivergenceEvent, WhatIfResult, run_what_if

__all__ = ["run_what_if", "WhatIfResult", "DivergenceEvent"]

