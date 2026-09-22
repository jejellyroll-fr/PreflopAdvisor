#!/usr/bin/env python3
"""Which simulation a real hand belongs to, and how close is close enough.

Everything here runs without Qt: the metadata model, the policy and the catalog are what an
integration outside this application would call, so they have to be usable from a script. The
simulations are stand-ins written against the provider protocol -- the point of the layer is the
*judgement*, and pinning that against a real export would test that export's numbers as much as
the judgement -- plus one real CSV table, to prove the sizing and rake facts are read off an
actual simulation rather than only declared.

The cases are the ones the issue names: an exact stack and rake, a nearby stack, a distant one,
a sizing the tree does not hold, a rake that does not match, heads-up against six-handed, and
several candidates that are equally good.
"""

import pytest

from preflop_advisor.simulation_catalog import (
    APPROXIMATE,
    CLOSE,
    EXACT,
    INCOMPATIBLE,
    UNKNOWN,
    Candidate,
    Compatibility,
    Policy,
    Rake,
    SimulationCatalog,
    SimulationQuery,
    catalog_of,
    declared_meta,
    detect_open_sizings,
    entries_of,
    kind_of_action,
    meta_from,
    meta_of,
    profile_value,
    read_profiles,
    write_meta,
    write_profiles,
)

from .test_csv_strategy import provider_for as csv_provider
from .test_csv_strategy import write_table
from .test_hand_review import FakeSimulation, act, hand_of

# --------------------------------------------------------------------------------------
# A catalog of stand-ins


def catalog_over(*providers, declared=None, profiles=None, policy=None):
    """A catalog over stand-in simulations, each named after its position."""
    candidates = [
        Candidate(
            name=f"sim{index}",
            provider=provider,
            label=f"sim{index}",
            declared=(declared[index] if declared else {}),
        )
        for index, provider in enumerate(providers)
    ]
    return catalog_of(candidates, profiles=profiles, policy=policy)


def query_of(**fields):
    """A real hand's environment, heads-up at 100bb with the usual 2.5bb open."""
    fields.setdefault("game", "PLO")
    fields.setdefault("players", 2)
    fields.setdefault("effective_stack_bb", 100.0)
    fields.setdefault("observed_sizings", (2.5,))
    return SimulationQuery(**fields)


def judged(catalog, query=None, override=None):
    """The one judgement in a catalog of one, which is most of these tests."""
    report = catalog.rank(query or query_of(), override=override)
    assert report.matches
    return report


class Store:
    """The slice of the layered configuration the catalog writes through, in memory.

    Written against the same three methods the window's configuration offers, which is the
    whole of what this module is allowed to assume about a user's storage.
    """

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.saves = 0

    def set(self, section, key, value):
        self.values[(section.lower(), key.lower())] = value

    def reset(self, section, key):
        self.values.pop((section.lower(), key.lower()), None)

    def keys(self, section):
        return [key for (name, key) in self.values if name == section.lower()]

    def save(self):
        self.saves += 1

    def section(self, name):
        return {key: value for (section, key), value in self.values.items() if section == name.lower()}


# --------------------------------------------------------------------------------------
# Metadata: facts, labels, and which is which


def test_a_simulation_knows_which_of_its_facts_it_stated_and_which_a_user_did():
    catalog = catalog_over(FakeSimulation(), declared=[{"rake_percent": "4.5", "aliases": "PokerStars PLO50"}])
    meta = judged(catalog).matches[0]

    assert meta.of("game") is not None
    entry = catalog.entries[0].meta
    assert entry.origin("game") == "detected"
    assert entry.origin("rake") == "declared"
    assert entry.origin("bb_bb") == "assumed", "a blind nobody stated is not a declaration"
    assert entry.rake.percent == pytest.approx(4.5)
    assert entry.aliases == ("PokerStars PLO50",)


def test_the_identity_of_a_strategy_is_its_poker_parameters_and_not_the_room_on_it():
    """Two rooms whose rake and structure agree are one solved tree."""
    base = meta_from("Table5", "ranges/hu", {"rake_percent": "4.5", "aliases": "PokerStars PLO50"})
    elsewhere = meta_from("Table6", "ranges/other", {"rake_percent": "4.5", "aliases": "GG PLO50"})

    assert base.identity() == elsewhere.identity()
    assert base.fingerprint() == elsewhere.fingerprint()
    assert base.label != elsewhere.label, "and the labels are still the user's own"


def test_a_different_rake_is_a_different_strategy():
    first = meta_from("Table5", "ranges/hu", {"rake_percent": "4.5"})
    second = meta_from("Table5", "ranges/hu", {"rake_percent": "9"})

    assert first.fingerprint() != second.fingerprint()


def test_what_nobody_declared_is_reported_as_missing_rather_than_assumed():
    meta = meta_from("Table5", "ranges/hu", {}, open_sizings=(2.5,))

    assert "rake" in meta.missing()
    assert "cash or tournament" in meta.missing()
    assert meta.complete is False
    assert (
        "rake 4.5%" in meta_from("Table5", "r", {"rake_percent": "4.5", "context": "cash", "version": "1.9"}).identity()
    )


def test_a_rake_profile_resolves_the_percentage_for_every_simulation_that_names_it():
    profiles = read_profiles({"PS_PLO50": "4.5, 3, bb"})
    meta = meta_from("Table5", "r", {"rake_profile": "PS_PLO50"}, profiles=profiles)

    assert meta.rake.percent == pytest.approx(4.5)
    assert meta.rake.cap == pytest.approx(3.0)
    assert meta.rake.cap_unit == "bb"
    assert profiles["ps_plo50"].describe() == "4.5% cap 3bb profile ps_plo50"


def test_a_profile_nothing_declares_simply_does_not_resolve():
    meta = meta_from("Table5", "r", {"rake_profile": "PS_PLO999"}, profiles={})

    assert meta.rake.percent is None
    assert meta.rake.profile == "PS_PLO999", "and the name is kept, so the user can see the typo"


def test_a_declared_fact_survives_a_round_trip_through_the_configuration():
    store = Store()
    meta = meta_from(
        "Table5",
        "ranges/hu",
        {
            "rake_percent": "4.5",
            "rake_cap": "3",
            "rake_cap_unit": "bb",
            "context": "cash",
            "solver": "MonkerSolver",
            "version": "1.9",
            "sb_bb": "0.5",
            "bb_bb": "1",
            "aliases": "PokerStars PLO50, ps_plo_6max_midstakes",
            "tags": "mid",
            "notes": "exported in May",
            "enabled": "no",
        },
    )

    write_meta(store, "Table5", meta)
    read = declared_meta("Table5", store.section("TreeInfos"))

    assert read["rake_percent"] == "4.5"
    assert read["aliases"] == "PokerStars PLO50, ps_plo_6max_midstakes"
    assert read["enabled"] == "no"
    assert store.saves == 1
    assert declared_meta("Table5", store.section("TreeInfos")) == read
    assert declared_meta("Table6", store.section("TreeInfos")) == {}


def test_an_undeclared_blind_is_not_written_as_a_declaration():
    """The dangerous half of writing metadata: a default frozen in looks exactly like a fact."""
    store = Store()
    meta = meta_from("Table5", "r", {"rake_percent": "4.5"})

    write_meta(store, "Table5", meta)

    assert store.section("TreeInfos").get("table5.sb_bb") is None
    assert store.section("TreeInfos").get("table5.bb_bb") is None


def test_clearing_a_declaration_removes_it_rather_than_blanking_it():
    store = Store({("treeinfos", "table5.rake_percent"): "4.5", ("treeinfos", "table5.rake_cap"): "3"})

    write_meta(store, "Table5", meta_from("Table5", "r", {"rake_percent": "4.5"}))

    assert store.section("TreeInfos")["table5.rake_percent"] == "4.5"
    assert "table5.rake_cap" not in store.section("TreeInfos"), "an empty key would read as a declared zero"


def test_profiles_are_written_and_the_ones_removed_are_dropped():
    store = Store({("rakeprofiles", "old_room"): "9,1,bb"})
    keep = {"PS_PLO50": Rake(percent=4.5, cap=3.0, cap_unit="bb", profile="PS_PLO50")}

    write_profiles(store, keep)

    assert store.section("RakeProfiles") == {"ps_plo50": "4.5,3,bb"}
    assert "old_room" not in store.section("RakeProfiles")
    assert profile_value(Rake(percent=None, cap=None)) == ",,bb"


# --------------------------------------------------------------------------------------
# The dimensions


def test_the_same_game_at_the_same_depth_and_rake_is_an_exact_match():
    catalog = catalog_over(FakeSimulation(), declared=[{"rake_percent": "4.5", "context": "cash"}])

    result = judged(catalog, query_of(rake=Rake(percent=4.5))).chosen

    assert result.status == EXACT
    assert result.comparable is True
    assert result.of("stack").detail == "100bb vs 100bb (+0bb)"
    assert result.of("players").detail == "HU"
    assert [warning for warning in result.warnings() if ": unknown" not in warning] == [], (
        "everything that was actually judged came back exact; the rest is what nobody stated"
    )
    assert result.warnings() == (
        "blinds: unknown (the hand does not state its blinds)",
        "room: unknown (the hand names no room or stake)",
    )


def test_a_nearby_stack_is_close_rather_than_the_same():
    catalog = catalog_over(FakeSimulation())

    result = judged(catalog, query_of(effective_stack_bb=97.0)).chosen

    assert result.status == CLOSE
    assert result.comparable is True
    assert result.of("stack").detail == "97bb vs 100bb (-3bb)"
    assert "stack: close" in result.explain()


def test_a_distant_stack_is_not_this_game_at_all():
    catalog = catalog_over(FakeSimulation())

    report = judged(catalog, query_of(effective_stack_bb=37.0))

    assert report.chosen is None
    assert report.matches[0].status == INCOMPATIBLE
    assert report.matches[0].placeable is False
    assert "no compatible simulation" in report.reason.lower()
    assert "37bb vs 100bb (-63bb)" in report.reason


def test_a_stack_between_the_bands_is_approximate_and_is_not_priced():
    catalog = catalog_over(FakeSimulation())

    result = judged(catalog, query_of(effective_stack_bb=78.0)).chosen

    assert result.status == APPROXIMATE
    assert result.comparable is False, "approximate candidates are shown, not priced"


def test_a_policy_may_price_an_approximate_match_or_refuse_it():
    catalog = catalog_over(FakeSimulation(), policy=Policy(allow_approximate_for_ev=True))

    assert judged(catalog, query_of(effective_stack_bb=78.0)).chosen.comparable is True


def test_heads_up_against_six_handed_is_never_the_same_game():
    catalog = catalog_over(FakeSimulation())

    result = judged(catalog, query_of(players=6)).matches[0]

    assert result.status == INCOMPATIBLE
    assert result.of("players").detail == "HU vs 6-max"
    assert result.placeable is False


def test_another_variant_is_never_the_same_game():
    catalog = catalog_over(FakeSimulation())

    result = judged(catalog, query_of(game="NL")).matches[0]

    assert result.of("game").detail == "PLO vs NL"
    assert result.status == INCOMPATIBLE


def test_an_ante_the_simulation_does_not_state_is_unknown_and_not_a_mismatch():
    catalog = catalog_over(FakeSimulation(ante_bb=None))

    result = judged(catalog, query_of(ante_bb=0.125)).matches[0]

    assert result.of("ante").status == UNKNOWN
    assert result.of("ante").detail == "this simulation has an ante it does not size"
    assert result.placeable is True, "an unknown is not a refusal"


def test_an_ante_the_two_disagree_about_is_a_different_pot_for_every_sizing():
    catalog = catalog_over(FakeSimulation(ante_bb=0.0))

    result = judged(catalog, query_of(ante_bb=0.125)).matches[0]

    assert result.of("ante").status == INCOMPATIBLE
    assert result.of("ante").detail == "ante 0bb vs 0.125bb"


def test_a_sizing_the_tree_holds_within_a_rounding_is_the_same_line():
    catalog = catalog_over(FakeSimulation())

    assert judged(catalog, query_of(observed_sizings=(2.47,))).chosen.of("sizings").status == EXACT
    assert judged(catalog, query_of(observed_sizings=(2.75,))).chosen.of("sizings").status == CLOSE


def test_an_open_the_tree_does_not_hold_marks_the_simulation_unusable_for_that_line():
    """Still walked, so the node matcher can name the action that is missing."""
    catalog = catalog_over(FakeSimulation())

    result = judged(catalog, query_of(observed_sizings=(4.0,))).matches[0]

    assert result.of("sizings").status == INCOMPATIBLE
    assert result.of("sizings").fatal is False
    assert result.placeable is True
    assert result.comparable is False, "a line this tree does not hold is never priced"
    assert result.of("sizings").detail == "opened 4bb; this simulation opens 2.5bb"


def test_a_hand_that_never_raises_has_no_sizing_to_compare():
    catalog = catalog_over(FakeSimulation(), declared=[{"rake_percent": "4.5"}])

    result = judged(catalog, query_of(observed_sizings=(), rake=Rake(percent=4.5))).chosen

    assert result.of("sizings").status == "note"
    assert result.status == EXACT


def test_an_open_nobody_could_read_is_reported_rather_than_treated_as_a_mismatch():
    catalog = catalog_over(FakeSimulation(actions=("Call", "Fold")))

    result = judged(catalog).matches[0]

    assert result.of("sizings").status == UNKNOWN
    assert result.placeable is True


def test_a_rake_that_does_not_match_is_a_different_game():
    catalog = catalog_over(FakeSimulation(), declared=[{"rake_percent": "9"}])

    result = judged(catalog, query_of(rake=Rake(percent=2.0))).matches[0]

    assert result.of("rake").status == INCOMPATIBLE
    assert result.of("rake").detail == "9% vs 2%"
    assert result.placeable is False


def test_a_rake_the_simulation_never_declared_is_unknown_and_does_not_refuse_it():
    """Otherwise the feature would refuse every simulation whose rake was never written down."""
    catalog = catalog_over(FakeSimulation())

    result = judged(catalog, query_of(rake=Rake(percent=4.5))).matches[0]

    assert result.of("rake").status == UNKNOWN
    assert result.comparable is True
    assert "rake: unknown" in result.explain()
    assert judged(catalog, query_of(rake=Rake(percent=4.5))).chosen is not None


def test_a_policy_that_requires_a_rake_degrades_an_undeclared_one():
    catalog = catalog_over(FakeSimulation(), policy=Policy(require_rake=True))

    result = judged(catalog, query_of(rake=Rake(percent=4.5))).matches[0]

    assert result.of("rake").status == APPROXIMATE
    assert result.comparable is False


def test_the_same_percentage_capped_in_a_different_place_is_a_different_game():
    """A cap is part of what a game takes, and the percentage alone cannot see it.

    Both tables take 5%, and above the lower cap they take different amounts of every pot. A
    comparison reading only the percentage would call them one game and price a hand against
    the other's numbers.
    """
    catalog = catalog_over(FakeSimulation(), declared=[{"rake_percent": "5", "rake_cap": "1", "rake_cap_unit": "bb"}])

    result = judged(catalog, query_of(rake=Rake(percent=5.0, cap=10.0, cap_unit="bb"))).matches[0]

    assert result.of("rake").status == INCOMPATIBLE
    assert "cap 1bb vs cap 10bb" in result.of("rake").detail
    assert result.placeable is False


def test_a_cap_of_three_big_blinds_is_not_a_cap_of_three_chips():
    """The unit is carried rather than assumed, so the same number in another unit differs."""
    catalog = catalog_over(
        FakeSimulation(), declared=[{"rake_percent": "5", "rake_cap": "3", "rake_cap_unit": "chips"}]
    )

    result = judged(catalog, query_of(rake=Rake(percent=5.0, cap=3.0, cap_unit="bb"))).matches[0]

    assert result.of("rake").status == INCOMPATIBLE
    assert "cap 3chips vs cap 3bb" in result.of("rake").detail


def test_a_percentage_and_a_cap_that_agree_are_exact():
    catalog = catalog_over(FakeSimulation(), declared=[{"rake_percent": "5", "rake_cap": "3", "rake_cap_unit": "bb"}])

    result = judged(catalog, query_of(rake=Rake(percent=5.0, cap=3.0, cap_unit="bb"))).chosen

    assert result is not None
    assert result.of("rake").status == EXACT


def test_a_cap_only_one_side_states_is_not_a_mismatch():
    """A cap nobody wrote is not a cap of nothing, so the percentages are all there is to read."""
    catalog = catalog_over(FakeSimulation(), declared=[{"rake_percent": "5"}])

    result = judged(catalog, query_of(rake=Rake(percent=5.0, cap=3.0, cap_unit="bb"))).chosen

    assert result is not None
    assert result.of("rake").status == EXACT


def test_a_room_alias_may_stand_in_for_a_rake_the_simulation_does_not_state():
    catalog = catalog_over(FakeSimulation(), declared=[{"aliases": "PokerStars PLO50"}])

    result = judged(catalog, query_of(site="PokerStars", stake_label="PLO50", rake=Rake(percent=4.5))).matches[0]

    assert result.of("room").status == EXACT
    assert result.of("room").detail == "alias PokerStars PLO50"
    assert result.of("rake").status == CLOSE
    assert "stands in" in result.of("rake").detail
    assert result.comparable is True


def test_a_policy_may_forbid_an_alias_standing_in_for_a_rake():
    catalog = catalog_over(
        FakeSimulation(),
        declared=[{"aliases": "PokerStars PLO50"}],
        policy=Policy(aliases_replace_rake=False),
    )

    result = judged(catalog, query_of(site="PokerStars", stake_label="PLO50", rake=Rake(percent=4.5))).matches[0]

    assert result.of("rake").status == UNKNOWN


def test_a_room_that_does_not_match_is_reported_and_never_refuses_anything():
    catalog = catalog_over(FakeSimulation(), declared=[{"aliases": "PokerStars PLO50", "rake_percent": "4.5"}])

    result = judged(catalog, query_of(site="GG", stake_label="PLO50", rake=Rake(percent=4.5))).matches[0]

    assert result.of("room").status == "note"
    assert "no alias of PokerStars PLO50 is GG PLO50" in result.of("room").detail
    assert result.status == EXACT


def test_blinds_are_only_compared_when_the_hand_states_them():
    catalog = catalog_over(FakeSimulation(), declared=[{"rake_percent": "4.5", "bb_bb": "2", "sb_bb": "1"}])

    unknown = judged(catalog, query_of(rake=Rake(percent=4.5))).matches[0]
    stated = judged(catalog, query_of(rake=Rake(percent=4.5), sb_bb=1.0, bb_bb=2.0, blinds_stated=True)).matches[0]

    assert unknown.of("blinds").status == UNKNOWN
    assert stated.of("blinds").status == EXACT
    assert stated.status == EXACT


def test_other_stakes_are_the_same_strategy_in_big_blinds():
    catalog = catalog_over(FakeSimulation(), declared=[{"rake_percent": "4.5", "sb_bb": "1", "bb_bb": "2"}])

    result = judged(catalog, query_of(rake=Rake(percent=4.5), sb_bb=0.25, bb_bb=0.5, blinds_stated=True)).matches[0]

    assert result.of("blinds").status == CLOSE
    assert result.comparable is True


# --------------------------------------------------------------------------------------
# Ranking the candidates


def test_the_closest_of_several_candidates_is_the_one_chosen():
    catalog = catalog_over(FakeSimulation(stack_bb=100.0), FakeSimulation(stack_bb=94.0))

    report = judged(catalog, query_of(effective_stack_bb=95.0))

    assert report.chosen is not None and report.chosen.simulation_id == "sim1"
    assert [match.simulation_id for match in report.matches] == ["sim1", "sim0"]


def test_equal_candidates_are_taken_and_still_reported_as_rivals():
    """A choice the catalog may make, and a user should still be able to see."""
    catalog = catalog_over(FakeSimulation(), FakeSimulation())

    report = judged(catalog)

    assert report.needs_choice is False
    assert report.chosen is not None
    assert report.chosen.rivals == ("sim1",)


def test_a_policy_may_turn_equal_candidates_into_a_question():
    catalog = catalog_over(FakeSimulation(), FakeSimulation(), policy=Policy(auto_select_on_tie=False))

    report = judged(catalog)

    assert report.chosen is None
    assert report.needs_choice is True
    assert "are all exact" in report.reason
    assert "choose one" in report.reason


def test_a_disabled_simulation_takes_part_in_nothing():
    catalog = catalog_over(FakeSimulation(), FakeSimulation(), declared=[{}, {"enabled": "no"}])

    report = judged(catalog)

    assert [match.simulation_id for match in report.matches] == ["sim0"]


def test_a_simulation_whose_files_cannot_be_read_is_listed_and_is_not_usable():
    entries = entries_of([{"table_key": "Table5", "plrs": 2, "bb": 100, "game": "PLO", "folder": "/nowhere"}], {})

    assert len(entries) == 1
    assert "could not be opened" in entries[0].note
    assert entries[0].indexed is False
    assert entries[0].meta.game == "PLO", "the tree entry's own fields are still read"
    assert entries[0].meta.origin("game") == "detected", "and still attributed to the entry, not to a user"
    report = SimulationCatalog(entries).rank(query_of())
    assert report.matches[0].status == INCOMPATIBLE
    assert report.matches[0].of("availability").detail.startswith("could not be opened")


def test_an_empty_catalog_says_so_rather_than_naming_a_simulation():
    report = SimulationCatalog([]).rank(query_of())

    assert report.chosen is None
    assert report.matches == ()
    assert report.reason == "No simulation is configured to compare a hand against."


def test_a_manual_override_uses_a_simulation_the_catalog_would_not_have_chosen():
    catalog = catalog_over(FakeSimulation(stack_bb=100.0), FakeSimulation(stack_bb=40.0))

    report = catalog.rank(query_of(), override="sim1")

    assert report.overridden is True
    assert report.chosen is not None
    assert report.chosen.simulation_id == "sim1"
    assert report.chosen.overridden is True
    assert report.chosen.comparable is False, "the warnings are kept, not washed off by being insisted past"
    assert "Chosen by hand" in report.chosen.explain()
    assert "cannot be used" in report.reason


def test_an_override_naming_a_simulation_that_is_not_configured_is_refused():
    report = catalog_over(FakeSimulation(), declared=[{"rake_percent": "4.5"}]).rank(
        query_of(rake=Rake(percent=4.5)), override="Table99"
    )

    assert report.chosen is None
    assert "Table99" in report.reason


# --------------------------------------------------------------------------------------
# Reading a simulation


def test_the_open_sizes_are_read_off_the_simulation_rather_than_declared():
    assert detect_open_sizings(FakeSimulation()) == (2.5,)


def test_a_real_table_opens_for_what_its_own_rows_say(tmp_path):
    write_table(tmp_path)

    assert detect_open_sizings(csv_provider(tmp_path)) == (2.5,)


def test_the_four_kinds_of_action_are_read_from_how_they_are_written():
    assert kind_of_action("folds") == "Fold"
    assert kind_of_action("raises") == "Raise"
    assert kind_of_action("Raise75") == "Raise"
    assert kind_of_action("all-in") == "AllIn"
    assert kind_of_action("checks") == "Check"
    assert kind_of_action("calls") == "Call"
    assert kind_of_action("mystery") == "Raise"


def test_a_hand_answers_a_query_with_what_it_knows():
    hand = hand_of(act("SB", "Raise", 2.5), hero="SB", site="PokerStars", stake_label="PLO50")

    query = SimulationQuery.from_hand(hand)

    assert query.game == "PLO"
    assert query.players == 2
    assert query.effective_stack_bb == pytest.approx(100.0)
    assert query.observed_sizings == (2.5,)
    assert query.alias_labels() == ("PokerStars PLO50", "PLO50", "PokerStars")
    assert "PLO HU 100bb" in query.describe()


def test_a_query_without_a_room_has_no_alias_to_match_on():
    query = SimulationQuery.from_hand(hand_of(act("SB", "Fold"), hero="SB"))

    assert query.alias_labels() == ()
    assert judged(catalog_over(FakeSimulation()), query).matches[0].of("room").status == UNKNOWN


def test_the_same_hand_against_a_table_and_against_a_standin_is_judged_the_same(tmp_path):
    """Two providers, one judgement: the catalog knows nothing about a file format."""
    write_table(tmp_path)
    query = SimulationQuery.from_hand(hand_of(act("SB", "Raise", 2.5), hero="SB"))

    real = catalog_of([Candidate(name="Table1", provider=csv_provider(tmp_path))]).rank(query)
    standin = catalog_over(FakeSimulation()).rank(query)

    assert real.matches[0].status == standin.matches[0].status == EXACT
    assert real.matches[0].identity == standin.matches[0].identity


def test_a_candidate_carries_what_a_user_declared_without_a_configuration():
    candidate = Candidate(name="Table1", provider=FakeSimulation(), declared={"rake_percent": "4.5"})

    meta = meta_of(candidate)

    assert meta.rake.percent == pytest.approx(4.5)
    assert meta.origin("rake") == "declared"
    assert meta.origin("game") == "detected"


def test_a_compatibility_report_explains_itself_line_by_line():
    catalog = catalog_over(FakeSimulation(), declared=[{"rake_percent": "4.5", "aliases": "PokerStars PLO50"}])
    report = catalog.rank(
        query_of(
            effective_stack_bb=94.7,
            observed_sizings=(2.47,),
            rake=Rake(percent=4.5),
            site="PokerStars",
            stake_label="PLO50",
        )
    )

    lines = report.chosen.explain().splitlines()

    assert lines[0] == "Simulation: sim0"
    assert lines[1] == "Status: close"
    assert any("stack: close (94.7bb vs 100bb (-5.3bb))" in line for line in lines)
    assert any("room: exact (alias PokerStars PLO50)" in line for line in lines)
    assert any("EV comparison: enabled" in line for line in lines)
    assert "Hand: PLO HU 94.7bb PokerStars PLO50" in report.describe()


def test_a_judgement_is_a_value_object_a_caller_can_read_field_by_field():
    result = judged(catalog_over(FakeSimulation())).chosen

    assert isinstance(result, Compatibility)
    assert isinstance(result.of("game"), type(result.of("players")))
    assert result.of("nowhere") is None
