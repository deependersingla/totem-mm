"""State representation and transition logic for cricket DP."""

from dataclasses import dataclass
from typing import NamedTuple


class DPState(NamedTuple):
    """Minimal state for DP backward induction (chase innings)."""
    balls_remaining: int  # 0-120
    runs_needed: int      # 0-350 (0 or negative = won)
    wickets_in_hand: int  # 0-10


# Outcome types for transitions
OUTCOMES = ["dot", "single", "double", "triple", "four", "six", "wicket", "wide", "noball"]
OUTCOME_RUNS = {"dot": 0, "single": 1, "double": 2, "triple": 3, "four": 4, "six": 6, "wicket": 0, "wide": 1, "noball": 1}
OUTCOME_CONSUMES_BALL = {"dot": True, "single": True, "double": True, "triple": True, "four": True, "six": True, "wicket": True, "wide": False, "noball": False}
OUTCOME_IS_WICKET = {"dot": False, "single": False, "double": False, "triple": False, "four": False, "six": False, "wicket": True, "wide": False, "noball": False}


def transition(state: DPState, outcome: str) -> DPState:
    """Compute next state given current state and ball outcome."""
    runs = OUTCOME_RUNS[outcome]
    consumes = OUTCOME_CONSUMES_BALL[outcome]
    is_wicket = OUTCOME_IS_WICKET[outcome]

    new_balls = state.balls_remaining - (1 if consumes else 0)
    new_runs = state.runs_needed - runs
    new_wickets = state.wickets_in_hand - (1 if is_wicket else 0)

    return DPState(
        balls_remaining=max(0, new_balls),
        runs_needed=new_runs,  # can go negative (won by more)
        wickets_in_hand=max(0, new_wickets),
    )


def is_terminal(state: DPState) -> bool:
    """Check if state is terminal (game decided)."""
    if state.runs_needed <= 0:
        return True  # Won
    if state.wickets_in_hand <= 0:
        return True  # All out
    if state.balls_remaining <= 0:
        return True  # Balls exhausted
    return False


def terminal_value(state: DPState) -> float:
    """Win probability at a terminal state."""
    if state.runs_needed <= 0:
        return 1.0  # Won
    if state.runs_needed == 0 and state.balls_remaining == 0:
        return 0.5  # Tie → super over (coin flip approx)
    return 0.0  # Lost (all out or balls exhausted with runs remaining)


@dataclass
class TransitionProbs:
    """Transition probabilities for a specific (batter, bowler, phase, venue) context."""
    dot: float = 0.0
    single: float = 0.0
    double: float = 0.0
    triple: float = 0.0
    four: float = 0.0
    six: float = 0.0
    wicket: float = 0.0
    wide: float = 0.0
    noball: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "dot": self.dot, "single": self.single, "double": self.double,
            "triple": self.triple, "four": self.four, "six": self.six,
            "wicket": self.wicket, "wide": self.wide, "noball": self.noball,
        }

    def normalize(self) -> "TransitionProbs":
        total = sum(self.as_dict().values())
        if total <= 0:
            return self
        factor = 1.0 / total
        return TransitionProbs(**{k: v * factor for k, v in self.as_dict().items()})

    @classmethod
    def from_phase_averages(cls, phase: str) -> "TransitionProbs":
        """Default T20 transition probabilities by phase (from historical averages)."""
        if phase == "powerplay":
            return cls(dot=0.340, single=0.260, double=0.055, triple=0.005,
                       four=0.130, six=0.065, wicket=0.045, wide=0.060, noball=0.040).normalize()
        elif phase == "middle":
            return cls(dot=0.380, single=0.265, double=0.060, triple=0.005,
                       four=0.095, six=0.055, wicket=0.040, wide=0.060, noball=0.040).normalize()
        else:  # death
            return cls(dot=0.310, single=0.220, double=0.045, triple=0.005,
                       four=0.120, six=0.095, wicket=0.060, wide=0.090, noball=0.055).normalize()
