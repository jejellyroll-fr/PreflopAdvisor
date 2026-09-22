#!/usr/bin/env python3
"""Real hands against the solver: what was matched, what was not, and what was claimed.

Everything here runs without Qt. The document layer is read from a mapping, the matching runs
against the protocol, and the handoff to training is the trainer's own ``Spot`` -- so the whole
loop from a played hand to a drillable decision is testable without a window.

Two kinds of simulation are used deliberately. The real CSV table proves the path works end to
end with a provider that reads files: sizings out of a folder, EVs out of a column. The fake
one, written against the protocol and nothing else, pins the cases a real export will not
offer on demand -- two stored raises a coin toss apart, a tree with no raise of the size that
was played, a tree that reports no EVs at all.
"""

import json

import pytest

from preflop_advisor.hand_review import (
    MIN_LOSS_BB,
    PAYLOAD_VERSION,
    Candidate,
    NodeMatcher,
    RealAction,
    RealHand,
    Tolerance,
    load_document,
    mistakes,
    parse_document,
    ranked_by_loss,
    review,
    review_document,
    review_hands,
    session_spots,
)
from preflop_advisor.sizings import Sizing
from preflop_advisor.strategy import Node, SimulationMetadata, StrategyResult

from .conftest import REFERENCE_HAND
from .test_csv_strategy import provider_for as csv_provider
from .test_csv_strategy import write_table

# --------------------------------------------------------------------------------------
# The two simulations


class FakeSimulation:
    """A simulation that answers with what the test told it to.

    Written against the protocol and nothing else, which is the point: the matcher is meant to
    work through a provider, so a fake provider is the honest way to test *matching*. Going
    through a real export would test that export's sizings as much as the matching.
    """

    def __init__(
        self,
        actions: tuple[str, ...] = ("Raise75", "Call", "Fold"),
        sizings: dict[str, Sizing] | None = None,
        seats: tuple[str, ...] = ("SB", "BB"),
        stack_bb: float = 100.0,
        game: str = "PLO",
        ante_bb: float | None = 0.0,
        strategy: dict[str, tuple[float, float | None]] | None = None,
    ) -> None:
        self.seats = tuple(seats)
        self.actions = tuple(actions)
        self.stack_bb = stack_bb
        self.game = game
        self.ante_bb = ante_bb
        self._sizings = dict(
            sizings
            if sizings is not None
            else {
                "raise75": Sizing("pot", 0.75),
                "raise80": Sizing("pot", 0.8),
                "call": Sizing("call", 0.0),
                "fold": Sizing("fold", 0.0),
            }
        )
        self._strategy = dict(strategy if strategy is not None else {action: (1.0, 0.0) for action in actions})

    def metadata(self) -> SimulationMetadata:
        return SimulationMetadata(
            game=self.game,
            num_players=len(self.seats),
            stack_bb=self.stack_bb,
            seats=self.seats,
            ante_bb=self.ante_bb,
            chips_per_bb=100.0,
        )

    def sizings(self) -> dict[str, Sizing]:
        return dict(self._sizings)

    def resolve(self, node: Node) -> Node | None:
        return Node(hero=node.hero, path=tuple(node.path)) if node.hero in self.seats else None

    def children(self, node: Node) -> list[Node]:
        return []

    def has_node(self, node: Node) -> bool:
        return node.hero in self.seats

    def strategy(self, node: Node, hand: str) -> tuple[StrategyResult, ...]:
        if node.hero not in self.seats:
            return ()
        return tuple(StrategyResult(action, frequency, ev) for action, (frequency, ev) in self._strategy.items())

    def hands_at(self, node: Node) -> list[str]:
        return [REFERENCE_HAND]


def matcher_over(*providers: object, tolerance: Tolerance | None = None) -> NodeMatcher:
    """A matcher over the given simulations, named after their class for the notes."""
    return NodeMatcher(
        [Candidate(name=f"sim{index}", provider=provider) for index, provider in enumerate(providers)],
        tolerance,
    )


# --------------------------------------------------------------------------------------
# Real hands, as a history writes them


def act(seat: str, action: str, to_bb: float | None = None) -> RealAction:
    return RealAction(seat=seat, action=action, to_bb=to_bb)


def hand_of(*actions: RealAction, hero: str = "SB", cards: str = REFERENCE_HAND, **fields: object) -> RealHand:
    """One real hand, heads-up unless the caller says otherwise."""
    fields.setdefault("table_size", 2)
    fields.setdefault("stack_bb", 100.0)
    fields.setdefault("seats", ("SB", "BB"))
    return RealHand(
        hand_id=str(fields.pop("hand_id", "h1")),
        hero=hero,
        hero_cards=cards,
        actions=tuple(actions),
        **fields,  # type: ignore[arg-type]
    )


def payload(*hands: dict, version: int = PAYLOAD_VERSION) -> dict:
    """A hand-review document, as the fpdb-3 side of the integration would write it."""
    return {"version": version, "hands": list(hands)}


def raw_hand(**fields: object) -> dict:
    """One hand of a document, with the defaults a real export would fill in."""
    entry: dict = {
        "hand_id": "h1",
        "played_at": "2024-05-01 20:15",
        "hero": "SB",
        "hero_cards": REFERENCE_HAND,
        "game": "PLO",
        "table_size": 2,
        "effective_stack_bb": 100.0,
        "ante_bb": 0.0,
        "seats": ["SB", "BB"],
        "actions": [
            {"seat": "SB", "action": "Raise", "to_bb": 2.5},
            {"seat": "BB", "action": "Fold"},
        ],
    }
    entry.update(fields)
    return entry


@pytest.fixture
def table(tmp_path):
    """A folder holding the CSV table the review is compared against."""
    write_table(tmp_path)
    return tmp_path


@pytest.fixture
def csv_simulation(table):
    return csv_provider(table)


# --------------------------------------------------------------------------------------
# Reading a document


def test_a_document_is_read_into_the_hands_it_holds():
    hands, report = parse_document(payload(raw_hand(), raw_hand(hand_id="h2")))

    assert [hand.hand_id for hand in hands] == ["h1", "h2"]
    assert report.hands == 2
    assert report.problems == ()
    assert report.version == PAYLOAD_VERSION


def test_a_payload_from_another_version_is_refused():
    """Its fields would read as blanks, which is worse than refusing the file."""
    with pytest.raises(ValueError, match="version 2"):
        parse_document(payload(raw_hand(), version=2))


def test_a_payload_that_holds_no_list_of_hands_is_refused():
    with pytest.raises(TypeError, match="list of hands"):
        parse_document({"version": PAYLOAD_VERSION})


def test_a_hand_that_cannot_be_read_is_a_problem_beside_the_hands_that_did():
    hands, report = parse_document(payload(raw_hand(), raw_hand(hand_id="h2", hero_cards="")))

    assert [hand.hand_id for hand in hands] == ["h1"]
    assert len(report.problems) == 1
    assert "hand 2" in report.problems[0]


def test_a_hand_whose_hero_never_acts_is_not_read():
    hands, report = parse_document(payload(raw_hand(actions=[{"seat": "BB", "action": "Fold"}])))

    assert hands == []
    assert "has no action in this hand" in report.problems[0]


def test_a_document_that_is_not_json_is_refused(tmp_path):
    path = tmp_path / "not.json"
    path.write_text("hands: none", encoding="utf-8")

    with pytest.raises(ValueError, match="not JSON"):
        load_document(path)


def test_the_hero_s_hand_may_be_the_export_s_own_key():
    hands, _ = parse_document(payload(raw_hand(hero_cards="(3K)(4A)")))

    assert hands[0].hand_key == "(3K)(4A)"


# --------------------------------------------------------------------------------------
# The decisions of a hand


def test_only_the_hero_s_turns_become_decisions():
    hand = hand_of(act("SB", "Raise", 2.5), act("BB", "Call"), act("SB", "Check"))

    decisions = hand.decisions()

    assert [decision.index for decision in decisions] == [0, 2]
    assert [decision.taken.action for decision in decisions] == ["Raise", "Check"]


def test_a_decision_carries_the_pot_it_was_made_into():
    """The blinds, then what the two of them put in: the small blind's 3 and the big blind's 3."""
    hand = hand_of(act("SB", "Raise", 3.0), act("BB", "Call"), act("SB", "Check"))

    first, second = hand.decisions()

    assert first.pot_bb == pytest.approx(1.5), "the blinds, before anyone acts"
    assert second.pot_bb == pytest.approx(6.0), "0.5 + 2.5 + 3"


def test_a_hand_that_names_no_seating_order_has_no_pot():
    """The blinds are the last two seats of the order; without it, nobody knows whose they are."""
    hand = hand_of(act("SB", "Raise", 3.0), act("BB", "Call"), seats=())

    assert [decision.pot_bb for decision in hand.decisions()] == [None]


def test_a_raise_with_no_amount_takes_the_pot_with_it():
    """A pot computed as though the raise had been a call would be smaller than the real one."""
    hand = hand_of(act("SB", "Raise"), act("BB", "Call"), act("SB", "Check"))

    first, second = hand.decisions()

    assert first.pot_bb == pytest.approx(1.5)
    assert second.pot_bb is None


def test_a_decision_names_the_family_of_line_it_belongs_to():
    hand = hand_of(act("SB", "Raise", 3.0), act("BB", "Call"), act("SB", "Check"), hero="BB")

    decisions = hand.decisions()

    assert decisions[0].family == "defend"
    assert decisions[0].hero == "BB"


# --------------------------------------------------------------------------------------
# Matching: what the tree holds, and how exactly


def test_a_raise_of_the_size_the_tree_holds_is_an_exact_match(csv_simulation):
    """The table's ``raise 75%`` is a raise to 2.5bb from the small blind, and 2.5 is what was played."""
    decisions = hand_of(act("SB", "Raise", 2.5), act("BB", "Fold")).decisions()

    match = matcher_over(csv_simulation).match(decisions[0])

    assert match.status == "exact"
    assert match.taken_action == "Raise75"
    assert match.node is not None
    assert match.node.hero == "SB"


def test_a_raise_within_the_tolerance_is_matched_as_sized():
    """The real world rounds the amount; the export does not round its buttons."""
    decisions = hand_of(act("SB", "Raise", 2.6)).decisions()

    match = matcher_over(FakeSimulation()).match(decisions[0])

    assert match.status == "sized"
    assert match.taken_action == "Raise75"
    assert match.matched is True


def test_the_sizing_tolerance_is_the_whole_of_what_approximate_means():
    decisions = hand_of(act("SB", "Raise", 2.62)).decisions()
    tight = matcher_over(FakeSimulation(), tolerance=Tolerance(blinds=0.05))

    match = tight.match(decisions[0])

    assert match.status == "unsupported"
    assert match.matched is False
    assert "no raise to 2.62bb" in match.note


def test_two_stored_raises_a_coin_toss_apart_are_ambiguous():
    """75% and 80% of a pot are 2.5bb and 2.6bb here: 2.55bb is genuinely either."""
    simulation = FakeSimulation(actions=("Raise75", "Raise80", "Call"))
    decisions = hand_of(act("SB", "Raise", 2.55)).decisions()

    match = matcher_over(simulation).match(decisions[0])

    assert match.status == "ambiguous"
    assert match.matched is False
    assert "Raise75, Raise80" in match.note
    assert match.taken_action is None, "an ambiguous read names no action"


def test_a_raise_the_tree_does_not_hold_is_not_rounded_onto_one_it_does():
    """8bb is not 75% of this pot, and calling it one would invent a strategy."""
    decisions = hand_of(act("SB", "Raise", 8.0)).decisions()

    match = matcher_over(FakeSimulation()).match(decisions[0])

    assert match.status == "unsupported"
    assert match.matched is False
    assert match.taken_action is None
    assert "no raise to 8bb here" in match.note


def test_a_raise_the_history_does_not_size_is_the_tree_s_own_when_it_holds_one():
    """Nothing to choose between, so the node is settled even though the size was not compared."""
    decisions = hand_of(act("SB", "Raise")).decisions()

    match = matcher_over(FakeSimulation(actions=("Raise75", "Call"))).match(decisions[0])

    assert match.status == "sized"
    assert match.taken_action == "Raise75"
    assert "does not say" in match.note


def test_a_raise_the_history_does_not_size_is_ambiguous_when_the_tree_holds_several():
    decisions = hand_of(act("SB", "Raise")).decisions()

    match = matcher_over(FakeSimulation(actions=("Raise75", "Raise80", "Call"))).match(decisions[0])

    assert match.status == "ambiguous"
    assert match.matched is False


def test_a_shove_is_matched_against_the_tree_s_own_all_in():
    simulation = FakeSimulation(actions=("AllIn", "Fold"), sizings={"allin": Sizing("allin"), "fold": Sizing("fold")})
    decisions = hand_of(act("SB", "AllIn", 100.0)).decisions()

    match = matcher_over(simulation).match(decisions[0])

    assert match.status == "exact"
    assert match.taken_action == "AllIn"


#: A three-handed table in acting order, blinds last -- which is how a provider names its
#: seats, and the order that decides who has posted what.
THREE_HANDED = ("BU", "SB", "BB")


def test_an_earlier_raise_is_identified_by_its_own_amount():
    """The fix this test exists for: ``Raise`` and ``Raise75`` are not the same node.

    A line read by *kind* would resolve to whichever sizing the tree happened to hold first, so
    a hand that raised to 2.5bb and a hand that raised to 8bb would be reviewed against one
    node. The node here names the sizing the amount came to.
    """
    simulation = FakeSimulation(seats=THREE_HANDED)
    decisions = hand_of(
        act("BU", "Fold"), act("SB", "Raise", 2.5), act("BB", "Call"), hero="BB", table_size=3, seats=THREE_HANDED
    ).decisions()

    match = matcher_over(simulation).match(decisions[0])

    assert match.status == "exact"
    assert match.node is not None
    assert match.node.hero == "BB"
    assert match.node.path == (("BU", "Fold"), ("SB", "Raise75")), "the sizing, not the kind"


def test_an_earlier_raise_the_tree_does_not_hold_stops_the_line():
    """The node behind a raise no stored action comes to is not the node behind a holdable one."""
    simulation = FakeSimulation(seats=THREE_HANDED)
    decisions = hand_of(
        act("BU", "Fold"), act("SB", "Raise", 5.0), act("BB", "Call"), hero="BB", table_size=3, seats=THREE_HANDED
    ).decisions()

    match = matcher_over(simulation).match(decisions[0])

    assert match.status == "unsupported"
    assert match.node is None
    assert "no raise to 5bb here for SB" in match.note


def test_a_line_that_reaches_a_seat_the_tree_does_not_have_is_unsupported():
    simulation = FakeSimulation(seats=THREE_HANDED)
    decisions = hand_of(
        act("CO", "Fold"), act("SB", "Raise", 2.5), act("BB", "Call"), hero="BB", table_size=3, seats=THREE_HANDED
    ).decisions()

    match = matcher_over(simulation).match(decisions[0])

    assert match.status == "unsupported"
    assert "CO, who is not seated" in match.note


def test_a_hand_of_another_table_size_has_no_compatible_simulation():
    """And the note names the nearest simulation and what it disagreed about."""
    decisions = hand_of(act("SB", "Raise", 2.5), hero="SB", table_size=6).decisions()

    match = matcher_over(FakeSimulation()).match(decisions[0])

    assert match.status == "no simulation"
    assert "6-max" in match.note
    assert "HU vs 6-max" in match.note
    assert match.matched is False


def test_a_simulation_of_another_depth_is_not_this_hand():
    decisions = hand_of(act("SB", "Raise", 2.5), stack_bb=130.0).decisions()

    assert matcher_over(FakeSimulation(stack_bb=100.0)).match(decisions[0]).status == "no simulation"
    assert (
        matcher_over(FakeSimulation(stack_bb=100.0), tolerance=Tolerance(stack_bb=40.0)).match(decisions[0]).status
        == "exact"
    )


def test_a_tree_that_does_not_size_its_ante_prices_nothing():
    """Every amount on the table is built on the ante, so no raise can be compared without it."""
    decisions = hand_of(act("SB", "Raise", 2.5)).decisions()

    match = matcher_over(FakeSimulation(ante_bb=None)).match(decisions[0])

    assert match.status == "unsupported"
    assert "ante" in match.note


def test_a_simulation_of_another_ante_structure_is_not_this_hand():
    """The ante is dead money in every pot, so two trees that differ by it are two games.

    Compatible on game, table size and depth alone, a fold or a call would be reported as an
    exact match -- carrying the EV of a solution solved for a different pot, with nothing
    about the answer looking wrong.
    """
    decisions = hand_of(act("SB", "Fold"), ante_bb=0.5).decisions()

    assert matcher_over(FakeSimulation(ante_bb=0.0)).match(decisions[0]).status == "no simulation"
    assert matcher_over(FakeSimulation(ante_bb=0.25)).match(decisions[0]).status == "no simulation"
    assert matcher_over(FakeSimulation(ante_bb=0.5)).match(decisions[0]).status == "exact"
    assert "ante" in matcher_over(FakeSimulation(ante_bb=0.0)).match(decisions[0]).note


def test_a_raise_is_compared_without_the_ante_the_seat_posted():
    """A history records what a raise came to in the betting; the ante is not part of it.

    Counted together, a raise to four in a half-blind-ante game reads as a raise to four and
    a half: a raise the tree holds is reported as one it does not.
    """
    simulation = FakeSimulation(
        actions=("RaisePot", "Fold"),
        sizings={"raisepot": Sizing("pot", 1.0), "fold": Sizing("fold", 0.0)},
        strategy={"RaisePot": (1.0, 0.0), "Fold": (0.0, 0.0)},
        ante_bb=0.5,
    )
    decisions = hand_of(act("SB", "Raise", 4.0), ante_bb=0.5).decisions()

    match = matcher_over(simulation).match(decisions[0])

    assert match.status == "exact", "the pot-sized raise from the small blind is to 4bb"
    assert match.taken_action == "RaisePot"


def test_the_closest_depth_wins_over_the_one_listed_first():
    """A 90bb tree beside the 100bb one is a near-miss, and a near-miss is not the hand.

    Both are inside the tolerance, so both are candidates -- but the hand was played at one
    of them, and the first one the configuration happens to list is not it.
    """
    decisions = hand_of(act("SB", "Fold")).decisions()

    match = matcher_over(
        FakeSimulation(stack_bb=90.0),
        FakeSimulation(stack_bb=100.0),
        tolerance=Tolerance(stack_bb=20.0),
    ).match(decisions[0])

    assert match.status == "exact"
    assert match.simulation == "sim1", "the tree at the depth the hand was played at"


def test_only_the_simulation_the_catalog_chose_is_walked():
    """A lower-ranked tree holding the node is not the hand's environment.

    Both sit inside the stack tolerance and the hand was played at one of them. Letting the
    other answer because it is the one with a node on this line would price the hand against
    another table's numbers while every ranking said the closer tree was the one in use.
    """
    decisions = hand_of(act("SB", "Raise", 2.5)).decisions()

    match = matcher_over(
        FakeSimulation(stack_bb=100.0, strategy={}),
        FakeSimulation(stack_bb=90.0),
        tolerance=Tolerance(stack_bb=20.0),
    ).match(decisions[0])

    assert match.status == "no node"
    assert match.simulation == "sim0", "the tree at the depth the hand was played at"
    assert "nothing for this hand" in match.note


def test_cards_that_do_not_convert_are_unsupported():
    """Two cards are a hold'em hand, not a four-card one, and nothing is claimed about it."""
    decisions = hand_of(act("SB", "Raise", 2.5), cards="AhKs").decisions()

    match = matcher_over(FakeSimulation()).match(decisions[0])

    assert match.status == "unsupported"
    assert "cannot be read" in match.note


def test_a_node_that_holds_nothing_for_the_hand_is_not_a_match():
    simulation = FakeSimulation(strategy={})
    decisions = hand_of(act("SB", "Raise", 2.5)).decisions()

    match = matcher_over(simulation).match(decisions[0])

    assert match.status == "no node"
    assert match.matched is False
    assert "nothing for this hand" in match.note


def test_a_call_is_matched_by_kind(csv_simulation):
    decisions = hand_of(act("SB", "Calls")).decisions()

    match = matcher_over(csv_simulation).match(decisions[0])

    assert match.status == "exact"
    assert match.taken_action == "Call"


# --------------------------------------------------------------------------------------
# What a match is worth


def test_a_matched_decision_says_what_the_action_cost(csv_simulation):
    """Folding a hand the solver raises: 1.2bb of expectation given up."""
    decisions = hand_of(act("SB", "Fold")).decisions()

    entry = review(decisions, matcher_over(csv_simulation))[0]

    assert entry.status == "exact"
    assert entry.taken_ev == pytest.approx(-0.5), "the EV is read in big blinds"
    assert entry.best_ev == pytest.approx(1.2)
    assert entry.best_action == "Raise75"
    assert entry.ev_loss_bb == pytest.approx(1.7)


def test_an_unmatched_decision_is_given_no_strategy_and_no_loss():
    decisions = hand_of(act("SB", "Raise", 8.0)).decisions()

    entry = review(decisions, matcher_over(FakeSimulation()))[0]

    assert entry.mix == ()
    assert entry.best_action is None
    assert entry.taken_ev is None
    assert entry.ev_loss_bb is None, "no number is better than a fabricated one"


def test_a_tree_that_reports_no_ev_gives_no_loss():
    simulation = FakeSimulation(strategy={"Raise75": (0.6, None), "Call": (0.4, None)})
    decisions = hand_of(act("SB", "Call")).decisions()

    entry = review(decisions, matcher_over(simulation))[0]

    assert entry.status == "exact"
    assert entry.mix, "the frequencies are there to show"
    assert entry.ev_loss_bb is None


def test_a_worse_decision_ranks_before_a_better_one_and_the_unpriced_come_last(csv_simulation):
    hands = [
        hand_of(act("SB", "Raise", 2.5), hand_id="fine"),
        hand_of(act("SB", "Fold"), hand_id="bad"),
        hand_of(act("SB", "Raise", 8.0), hand_id="unsized"),
    ]

    ranked = ranked_by_loss(review_hands(hands, matcher_over(csv_simulation)))

    assert [entry.decision.hand.hand_id for entry in ranked] == ["bad", "fine", "unsized"]


def test_mistakes_are_the_matched_losses_over_the_floor(csv_simulation):
    hands = [hand_of(act("SB", "Raise", 2.5), hand_id="fine"), hand_of(act("SB", "Fold"), hand_id="bad")]

    reviewed = review_hands(hands, matcher_over(csv_simulation))

    assert [entry.decision.hand.hand_id for entry in mistakes(reviewed, MIN_LOSS_BB)] == ["bad"]
    assert mistakes(reviewed, min_loss_bb=2.0) == []


# --------------------------------------------------------------------------------------
# From a reviewed mistake to a session


def test_the_spot_of_a_reviewed_decision_is_the_node_not_the_real_hand(csv_simulation):
    """Drilling the mistake means the decision, with whatever hands the node holds."""
    decisions = hand_of(act("SB", "Raise", 2.5)).decisions()

    entry = review(decisions, matcher_over(csv_simulation))[0]
    spot = entry.decision.spot(entry.match.node)

    assert spot.hero == "SB"
    assert spot.line == [], "the root decision is reached with nothing before it"
    assert "h1" in spot.label, "the hand it came from stays with it"


def test_a_session_takes_one_spot_per_node_worst_first(csv_simulation):
    """Two hands played badly on one decision are one thing to work on, not two."""
    hands = [
        hand_of(act("SB", "Fold"), hand_id="bad-one"),
        hand_of(act("SB", "Fold"), hand_id="bad-two"),
        hand_of(act("SB", "Raise", 2.5), hand_id="fine"),
    ]
    reviewed = review_hands(hands, matcher_over(csv_simulation))

    spots = session_spots(reviewed)

    assert len(spots) == 1, "one node, however many hands were played badly on it"
    assert spots[0].hero == "SB"


def test_a_session_takes_the_nodes_of_the_deepest_mistakes(csv_simulation):
    """Worst first, and each spot is *that* node: the big blind's decision is behind the raise."""
    hands = [
        hand_of(act("SB", "Fold"), hand_id="folded-the-button"),
        hand_of(act("SB", "Raise", 2.5), act("BB", "Call"), hand_id="called-the-raise", hero="BB"),
    ]
    reviewed = review_hands(hands, matcher_over(csv_simulation))

    spots = session_spots(reviewed)

    assert [spot.hero for spot in spots] == ["SB", "BB"], "1.7bb of loss before 0.1bb of it"
    assert [list(spot.line) for spot in spots] == [[], [("SB", "Raise75")]]
    assert session_spots(reviewed, limit=1) == spots[:1]


def test_a_review_counts_what_matched_and_what_did_not(tmp_path, table):
    path = tmp_path / "review.json"
    path.write_text(
        json.dumps(
            payload(
                raw_hand(hand_id="matched", actions=[{"seat": "SB", "action": "Fold"}]),
                raw_hand(hand_id="unsized", actions=[{"seat": "SB", "action": "Raise", "to_bb": 8.0}]),
                raw_hand(hand_id="six-handed", table_size=6, seats=["UTG", "MP", "CO", "BU", "SB", "BB"]),
            )
        ),
        encoding="utf-8",
    )

    review_of_file = review_document(path, matcher_over(csv_provider(table)))

    assert review_of_file.summary().splitlines()[0] == "Decisions reviewed: 3"
    assert review_of_file.by_status()["exact"] == 1
    assert review_of_file.by_status()["unsupported"] == 1
    assert review_of_file.by_status()["no simulation"] == 1
    assert len(review_of_file.matched) == 1
    assert len(review_of_file.unmatched) == 2
    assert "EV lost over 1 priced decisions: 1.70 bb" in review_of_file.summary()


def test_a_review_keeps_the_problems_the_document_had(tmp_path, table):
    path = tmp_path / "review.json"
    path.write_text(json.dumps(payload(raw_hand(), raw_hand(hero_cards=""))), encoding="utf-8")

    review_of_file = review_document(path, matcher_over(csv_provider(table)))

    assert review_of_file.report is not None
    assert review_of_file.report.problems
    assert review_of_file.report.hands == 1
