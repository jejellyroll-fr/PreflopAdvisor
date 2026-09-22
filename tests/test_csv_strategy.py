#!/usr/bin/env python3
"""A strategy table somebody else wrote: its columns, its numbers, and the nodes it holds.

Everything here runs without Qt. The format layer is a function per meaning, the provider is
a folder of files and a configuration, and the import path is the same wizard the range
folder goes through -- so a table that imports here is one the Advisor and the Trainer read.
"""

import csv
import os

import pytest

from preflop_advisor.config_store import LayeredConfig
from preflop_advisor.csv_format import (
    BLANKS,
    REQUIRED_ROLES,
    ColumnMapping,
    action_size,
    canonical_path,
    detect_columns,
    hand_key_of,
    is_raise,
    looks_like_cards,
    parse_action,
    parse_ev,
    parse_frequency,
    parse_line,
    read_rows,
)
from preflop_advisor.csv_provider import CsvIndex, CsvStrategyProvider, csv_files, fingerprint
from preflop_advisor.errors import CsvImportError, RangeFolderNotFound, SimulationScanError
from preflop_advisor.import_wizard import (
    ImportRequest,
    entry_value,
    inferred_mapping,
    register_simulation,
    scan_csv_simulation,
)
from preflop_advisor.paths import PACKAGE_ROOT
from preflop_advisor.settings import ConfigSource
from preflop_advisor.sizings import Sizing
from preflop_advisor.strategy import Node

from .conftest import PACKAGE_CONFIG

READER: ConfigSource = {"positions": "BB,SB,BU,CO,MP,UTG", "ChipsPerBB": "100"}

#: A small heads-up table: the small blind opens, the big blind answers it, and the small
#: blind acts again after the call. Written the way a script would write it -- sizes as
#: ``raise 75%`` and ``2.5bb``, shares as percentages, the EV column in big blinds.
TABLE = [
    ("", "SB", "AhKs4h3s", "raise 75%", "60", "1.2", "1.5"),
    ("", "SB", "AhKs4h3s", "call", "30", "1.1", "1.5"),
    ("", "SB", "AhKs4h3s", "fold", "10", "-0.5", "1.5"),
    ("SB:raise 75%", "BB", "AhKs4h3s", "raise 2.5bb", "40", "0.9", "4.0"),
    ("SB:raise 75%", "BB", "AhKs4h3s", "call", "60", "0.8", "4.0"),
    ("SB:raise 75%;BB:call", "SB", "AhKs4h3s", "check", "100", "0.5", "8.0"),
    ("SB:call", "BB", "AhKs4h3s", "check", "100", "0.4", "2.0"),
]
HEADER = ["Line", "Hero", "Hand", "Action", "Freq", "EV (bb)", "Pot"]
#: The other layout a converter emits: every raise is named ``Raise``, and its size is a
#: column of its own. Two raises of one node are then two rows under one name.
SIZING_HEADER = [*HEADER, "Sizing"]
SIZING_TABLE = [
    ("", "SB", "AhKs4h3s", "Raise", "60", "1.2", "1.5", "2.5bb"),
    ("", "SB", "AhKs4h3s", "Raise", "30", "1.1", "1.5", "8bb"),
    ("", "SB", "AhKs4h3s", "Call", "10", "0.5", "1.5", ""),
]


def write_table(folder, rows=TABLE, header=None, name="solution.csv", encoding="utf-8"):
    """One CSV file in a folder, with the header the rows are read under."""
    path = os.path.join(str(folder), name)
    header = HEADER if header is None else header
    with open(path, "w", newline="", encoding=encoding) as handle:
        writer = csv.writer(handle)
        if header:
            writer.writerow(header)
        writer.writerows(rows)
    return path


def tree_of(folder, columns=None, kind="csv") -> dict:
    """The tree entry a CSV simulation is configured as."""
    return {
        "plrs": 2,
        "bb": 100,
        "game": "PLO",
        "folder": str(folder),
        "infos": "test",
        "ante": 0.0,
        "kind": kind,
        "columns": dict(columns or {}),
    }


def provider_for(folder, columns=None, kind="csv") -> CsvStrategyProvider:
    """A provider over one folder of tables, seated by the shipped configuration."""
    return CsvStrategyProvider(tree_of(folder, columns, kind), READER)


@pytest.fixture
def folder(tmp_path):
    write_table(tmp_path)
    return tmp_path


# --------------------------------------------------------------------------------------
# Columns


def test_a_header_is_mapped_by_what_it_says_and_nothing_is_assigned_by_position():
    mapping = detect_columns(["Line of play", "Hero", "Hand key", "Action", "Freq", "EV (bb)", "Notes"])

    assert mapping.column_of("line") == "Line of play"
    assert mapping.column_of("hero") == "Hero"
    assert mapping.column_of("hand") == "Hand key"
    assert mapping.column_of("action") == "Action"
    assert mapping.column_of("frequency") == "Freq"
    assert mapping.column_of("ev") == "EV (bb)"
    assert mapping.complete is True
    assert mapping.unused == ("Notes",)


def test_a_header_that_says_nothing_leaves_a_required_role_unmapped():
    """An assumed column is worse than a blank one: nothing downstream can tell them apart."""
    mapping = detect_columns(["column1", "column2", "hero", "hand", "action", "freq"])

    assert mapping.missing == ("line",)
    assert mapping.complete is False


def test_every_required_role_is_named():
    assert REQUIRED_ROLES == ("line", "hero", "hand", "action", "frequency")


def test_a_declared_mapping_overrides_what_a_header_said_and_can_clear_a_role():
    mapping = detect_columns(HEADER).overridden({"action": "Decision", "ev": ""})

    assert mapping.column_of("action") == "Decision"
    assert mapping.column_of("ev") is None
    assert "Decision" not in mapping.unused


def test_a_mapping_reads_back_as_the_lines_the_configuration_stores():
    mapping = ColumnMapping(columns={"hero": "Hero", "hand": "Hand", "unknown": "x"})

    assert mapping.describe() == ("hero: Hero", "hand: Hand")


# --------------------------------------------------------------------------------------
# Actions


def test_an_action_is_named_the_way_the_model_names_it():
    """``raise 75%`` and ``Raise75`` are one action, and a name is what a line is read from."""
    assert parse_action("fold")[:2] == ("Fold", "Fold")
    assert parse_action("folds")[:2] == ("Fold", "Fold")
    assert parse_action("Call")[:2] == ("Call", "Call")
    assert parse_action("check")[:2] == ("Check", "Check")
    assert parse_action("all-in")[:2] == ("AllIn", "AllIn")
    assert parse_action("jam")[:2] == ("AllIn", "AllIn")
    assert parse_action("raise 75%")[0] == "Raise75"
    assert parse_action("0.75pot")[0] == "Raise75"
    assert parse_action("75")[0] == "Raise75"
    assert parse_action("Raise100")[0] == "Raise100"
    assert parse_action("raise to 2.5 bb")[0] == "Raise2.5bb"


def test_the_two_families_of_size_are_read_and_kept_apart():
    assert parse_action("Raise75")[2] == Sizing("pot", 0.75)
    assert parse_action("2.5bb")[2] == Sizing("blinds", 2.5)
    assert parse_action("100")[2] == Sizing("pot", 1.0)
    assert parse_action("2.5")[2] == Sizing("blinds", 2.5)


def test_a_raise_nobody_can_size_is_kept_as_unknown_rather_than_guessed():
    """A name is not a size: ``3xOpen`` becomes a raise the table never stated."""
    name, kind, sizing = parse_action("3xOpen")

    assert (name, kind) == ("3xOpen", "Raise")
    assert sizing.known is False


def test_a_size_is_read_back_out_of_a_stored_name():
    assert action_size("Raise75") == Sizing("pot", 0.75)
    assert action_size("Raise2.5bb") == Sizing("blinds", 2.5)
    assert action_size("Fold") == Sizing("fold")
    assert action_size("AllIn") == Sizing("allin")
    assert action_size("3xOpen").known is False


def test_only_a_raise_is_what_a_generic_raise_may_stand_for():
    assert is_raise("Raise100") is True
    assert is_raise("AllIn") is False
    assert is_raise("Call") is False


def test_an_action_with_nothing_in_it_is_refused():
    for blank in BLANKS:
        with pytest.raises(ValueError):
            parse_action(blank)


# --------------------------------------------------------------------------------------
# Lines


def test_a_line_is_read_however_its_steps_were_separated():
    assert parse_line("SB Raise100; BB Call") == [("SB", "Raise100"), ("BB", "Call")]
    assert parse_line("SB:Raise100,BB:Call") == [("SB", "Raise100"), ("BB", "Call")]
    assert parse_line("SB Raise100 -> BB Call") == [("SB", "Raise100"), ("BB", "Call")]
    assert parse_line("") == []


def test_a_node_written_out_with_its_hero_drops_that_prefix():
    assert parse_line("BB:SB Raise100", "BB") == [("SB", "Raise100")]
    assert parse_line("BB:BU Raise100;SB Raise300", "BB") == [("BU", "Raise100"), ("SB", "Raise300")]


def test_a_step_that_begins_with_the_hero_is_not_an_identity_prefix():
    """``SB:raise 75%;BB:call`` is a line the small blind opened, not a prefix to drop."""
    assert parse_line("SB:raise 75%;BB:call", "SB") == [("SB", "raise 75%"), ("BB", "call")]


def test_a_step_that_names_no_seat_and_action_is_refused():
    with pytest.raises(ValueError, match="names no seat"):
        parse_line("SB Raise100;garbage")


def test_a_line_is_stored_with_its_actions_named_canonically():
    assert canonical_path([("SB", "raise 75%"), ("BB", "call")]) == (("SB", "Raise75"), ("BB", "Call"))


# --------------------------------------------------------------------------------------
# Numbers


def test_a_share_of_one_is_read_from_every_spelling_of_it():
    assert parse_frequency("0.5") == 0.5
    assert parse_frequency("50") == 0.5
    assert parse_frequency("50%") == 0.5
    assert parse_frequency("1") == 1.0
    assert parse_frequency("0") == 0.0


def test_a_frequency_outside_zero_and_one_is_refused():
    with pytest.raises(ValueError, match="not a frequency"):
        parse_frequency("half")
    with pytest.raises(ValueError, match="no frequency"):
        parse_frequency("")


def test_an_ev_is_read_in_the_unit_its_header_names():
    assert parse_ev("1.2", 100.0, "auto", "EV (bb)") == pytest.approx(120.0)
    assert parse_ev("1.2", 100.0, "auto", "ev_chips") == pytest.approx(1.2)
    assert parse_ev("1.2", 100.0, "bb", "anything") == pytest.approx(120.0)
    assert parse_ev("1.2", 100.0, "chips", "ev_bb") == pytest.approx(1.2)


def test_a_blank_ev_is_not_a_zero_ev():
    for blank in ("", "-", "n/a", "NaN"):
        assert parse_ev(blank, 100.0) is None


def test_an_ev_that_is_not_a_number_is_refused():
    with pytest.raises(ValueError, match="not an EV"):
        parse_ev("about even", 100.0)


# --------------------------------------------------------------------------------------
# Hands


def test_a_hand_is_read_by_its_key_whichever_way_the_table_spelled_it():
    assert hand_key_of("AhKs4h3s") == "(3K)(4A)"
    assert hand_key_of("(3K)(4A)") == "(3K)(4A)"
    assert hand_key_of("3hKh4sAs") == "(3K)(4A)"


def test_a_key_that_looks_like_cards_is_read_as_the_key_it_is():
    """``KA23`` is a hand class of four ranks; converting it as cards gives another class."""
    assert looks_like_cards("KA23") is False
    assert hand_key_of("KA23") != "K2o", "read as cards, four ranks become a hold'em hand"
    assert hand_key_of("KA23") == hand_key_of(hand_key_of("KA23")), "normalising is idempotent"
    assert looks_like_cards("KhAh2s3s") is True


def test_a_hand_nothing_can_read_is_kept_as_written():
    assert hand_key_of("nonsense") == "nonsense"


# --------------------------------------------------------------------------------------
# Rows


def rows_of(rows, mapping=None, header=None, **kwargs):
    header = HEADER if header is None else header
    mapping = mapping or detect_columns(header)
    return read_rows((dict(zip(header, row)) for row in rows), mapping, chips_per_bb=100.0, **kwargs)


def test_a_raise_sized_by_a_column_of_its_own_is_named_by_that_size():
    """The action column says ``Raise`` on every row, so the size column is the difference.

    A name that kept the bare word would make one node's two raises a single action, and
    the index -- which keys actions by name -- would keep whichever it read first and price
    the other one by it.
    """
    accepted, problems = rows_of(SIZING_TABLE, header=SIZING_HEADER)

    assert problems == []
    assert [row.action for row in accepted] == ["Raise2.5bb", "Raise8bb", "Call"]
    assert [row.sizing for row in accepted] == [Sizing("blinds", 2.5), Sizing("blinds", 8.0), Sizing("call")]


def test_a_raise_the_export_names_itself_keeps_that_name_beside_a_size_column():
    """``Open`` is a word the table chose; only its size is taken from the other column.

    Its lines of play are written in that vocabulary, so renaming it would leave every line
    naming an action the node no longer offers.
    """
    rows = [("", "SB", "AhKs4h3s", "Open", "100", "1.2", "1.5", "2.5bb")]

    accepted, _ = rows_of(rows, header=SIZING_HEADER)

    assert accepted[0].action == "Open"
    assert accepted[0].sizing == Sizing("blinds", 2.5)


def test_a_row_is_read_into_one_action_of_one_node():
    accepted, problems = rows_of(TABLE[:3])

    assert problems == []
    assert len(accepted) == 3
    row = accepted[0]
    assert row.hero == "SB"
    assert row.path == ()
    assert row.action == "Raise75"
    assert row.frequency == pytest.approx(0.6)
    assert row.ev == pytest.approx(120.0)
    assert row.pot == pytest.approx(1.5)
    assert row.identity == "SB:"


def test_a_row_that_cannot_be_read_is_diagnosed_with_its_file_and_line():
    accepted, problems = rows_of([*TABLE[:3], ("", "BB", "", "call", "50", "0", "1")], file="solution.csv")

    assert len(accepted) == 3
    assert len(problems) == 1
    assert str(problems[0]) == "solution.csv:5: no hand"
    assert problems[0].line == 5


def test_a_row_whose_frequencies_do_not_sum_to_one_is_reported_and_read():
    """A table that lists only the actions taken is legitimate: reported, not refused."""
    accepted, problems = rows_of([("", "SB", "AhKs4h3s", "call", "50", "1", "1")])

    assert len(accepted) == 1
    assert len(problems) == 1
    assert "frequencies sum to 0.5000" in problems[0].message
    assert problems[0].fatal is False


def test_a_strict_import_refuses_what_a_lenient_one_reports():
    _, problems = rows_of([("", "SB", "AhKs4h3s", "call", "50", "1", "1")], strict=True)

    assert problems[0].fatal is True


def test_a_line_that_already_ends_with_the_hero_is_diagnosed():
    accepted, problems = rows_of([("BB:SB Raise100;BB Call", "BB", "AhKs4h3s", "fold", "100", "0", "1")])

    assert accepted == []
    assert "already ends with BB" in problems[0].message


# --------------------------------------------------------------------------------------
# The index


def test_reading_a_table_builds_an_index_beside_it(folder):
    index = CsvIndex(str(folder), {}, 100.0)
    report = index.ready()

    assert report.built is True
    assert report.usable is True
    assert os.path.isfile(os.path.join(str(folder), "preflop-csv.db"))
    assert report.files == ("solution.csv",)
    assert report.rows == len(TABLE)
    assert report.nodes == 4
    assert report.problem_total == 0
    index.close()


def test_an_index_is_not_rebuilt_while_the_tables_are_unchanged(folder):
    first = CsvIndex(str(folder), {}, 100.0)
    first.ready()
    first.close()

    second = CsvIndex(str(folder), {}, 100.0)
    report = second.ready()

    assert report.built is False
    assert report.rows == len(TABLE)
    second.close()


def test_editing_a_table_rebuilds_what_was_read_from_it(folder):
    first = CsvIndex(str(folder), {}, 100.0)
    first.ready()
    first.close()

    write_table(folder, rows=TABLE[:2])
    second = CsvIndex(str(folder), {}, 100.0)
    report = second.ready()

    assert report.built is True
    assert report.rows == 2
    second.close()


def test_a_declared_column_mapping_reads_a_table_nobody_could_guess(tmp_path):
    """``A`` and ``B`` say nothing; the declaration is what makes the table readable."""
    write_table(tmp_path, header=["A", "Hero", "Hand", "B", "Freq", "Ev", "Pot"])
    index = CsvIndex(str(tmp_path), {"line": "A", "action": "B"}, 100.0)
    report = index.ready()

    assert report.columns.column_of("action") == "B"
    assert report.rows == len(TABLE)
    assert report.usable is True


def test_a_folder_with_no_tables_is_refused(tmp_path):
    index = CsvIndex(str(tmp_path), {}, 100.0)

    with pytest.raises(CsvImportError, match="no .csv files"):
        index.ready()


def test_a_table_with_no_column_for_an_action_is_refused_with_its_header(tmp_path):
    write_table(tmp_path, header=["Line", "Hero", "Hand", "Freq", "Ev", "Pot"])
    index = CsvIndex(str(tmp_path), {}, 100.0)

    with pytest.raises(CsvImportError, match="no column for action"):
        index.ready()


def test_the_index_keeps_the_problems_it_found(folder):
    """A second read of an unchanged index still says which rows were left out."""
    write_table(folder, rows=[*TABLE, ("", "", "AhKs4h3s", "call", "50", "0", "1")])
    first = CsvIndex(str(folder), {}, 100.0)
    first.ready()
    first.close()

    again = CsvIndex(str(folder), {}, 100.0)
    report = again.ready()

    assert report.built is False
    assert report.problem_total == 1
    assert "no acting position" in report.problems[0].message
    assert "solution.csv:9: no acting position" in report.diagnostics()


def test_a_table_is_fingerprinted_to_the_nanosecond(tmp_path):
    """A same-size rewrite inside one second is still a rewrite.

    Read to the whole second, the fingerprint of an edited file is the fingerprint of the
    one before it -- same size, same second -- and the index goes on answering with the
    numbers the table no longer holds.
    """
    path = write_table(tmp_path, rows=TABLE[:1])
    files = csv_files(str(tmp_path))
    # A microsecond apart inside the same whole second, which is the smallest step every
    # filesystem in play can hold: a nanosecond would be rounded away on one of them and
    # the test would pass by measuring nothing.
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    first = fingerprint(files, {}, 100.0, "auto")

    os.utime(path, ns=(1_000_001_000, 1_000_001_000))
    assert int(os.stat(path).st_mtime) == 1, "the two have to share a second to be a subsecond test"

    assert fingerprint(files, {}, 100.0, "auto") != first


def test_the_files_of_a_folder_are_read_in_a_reproducible_order(tmp_path):
    write_table(tmp_path, rows=TABLE[:1], name="b.csv")
    write_table(tmp_path, rows=TABLE[:1], name="a.csv")

    assert [os.path.basename(path) for path in csv_files(str(tmp_path))] == ["a.csv", "b.csv"]


def test_two_tables_of_one_folder_may_name_the_action_column_differently(tmp_path):
    """Each table is read against its own header, whole, in one folder.

    Nothing a detection found is stored as a declaration: a folder whose files were written
    by different tools reads because every table is asked what its own columns say.
    """
    write_table(tmp_path, rows=TABLE, name="a.csv")
    write_table(tmp_path, rows=TABLE, header=["Move" if name == "Action" else name for name in HEADER], name="b.csv")

    report = provider_for(tmp_path).report

    assert report.rows == 2 * len(TABLE)
    assert report.problem_total == 0, "no table is read looking for another table's header"


def test_the_report_says_which_table_the_folder_was_read_with(tmp_path):
    """What the wizard shows as "columns read from this table" has to be one of them.

    The first table of the folder, since the files are listed in a reproducible order --
    which of them had the last word would otherwise be an accident of listing.
    """
    write_table(tmp_path, rows=TABLE, name="a.csv")
    write_table(tmp_path, rows=TABLE, header=["Move" if name == "Action" else name for name in HEADER], name="b.csv")

    report = provider_for(tmp_path).report

    assert report.columns.column_of("action") == "Action"
    assert report.columns.complete


# --------------------------------------------------------------------------------------
# The provider


def test_a_table_is_read_as_a_simulation(folder):
    metadata = provider_for(folder).metadata()

    assert metadata.game == "PLO"
    assert metadata.stack_bb == 100
    assert metadata.seats == ("SB", "BB")
    assert metadata.chips_per_bb == 100.0


def test_a_root_node_and_the_decision_behind_an_open_resolve(folder):
    provider = provider_for(folder)

    assert provider.resolve(Node("SB", [])) == Node("SB", ())
    assert provider.resolve(Node("BB", [("SB", "Raise")])) == Node("BB", (("SB", "Raise75"),))
    assert provider.has_node(Node("BB", [("SB", "Raise")])) is True
    assert provider.has_node(Node("BB", [("SB", "AllIn")])) is False


def test_a_seat_that_is_not_at_the_table_has_no_node(folder):
    assert provider_for(folder).resolve(Node("UTG", [])) is None


def test_a_node_says_one_action_per_thing_the_solver_may_do(folder):
    provider = provider_for(folder)

    results = provider.strategy(Node("BB", [("SB", "Raise")]), "AhKs4h3s")

    assert [result.action for result in results] == ["Raise2.5bb", "Call"]
    assert results[0].frequency == pytest.approx(0.4)
    assert results[0].ev == pytest.approx(90.0)


def test_an_action_the_table_prices_keeps_its_size(folder):
    sizings = provider_for(folder).sizings()

    assert sizings["Raise75"] == Sizing("pot", 0.75)
    assert sizings["Raise2.5bb"] == Sizing("blinds", 2.5)
    assert sizings["Check"] == Sizing("check", 0.0)


def test_the_hands_of_a_node_come_back_as_keys_the_trainer_can_deal(folder):
    assert provider_for(folder).hands_at(Node("BB", [("SB", "Raise")])) == ["(3K)(4A)"]


def test_a_hand_asked_as_cards_finds_the_key_the_table_stored(folder):
    """The trainer deals a concrete hand; a table of keys still has to answer it."""
    provider = provider_for(folder)

    assert provider.strategy(Node("SB", []), "3hKh4sAs") == provider.strategy(Node("SB", []), "(3K)(4A)")


def test_children_are_the_decisions_the_table_holds_behind_an_action(folder):
    provider = provider_for(folder)

    children = provider.children(Node("SB", []))

    assert children == [Node("BB", (("SB", "Raise75"),)), Node("BB", (("SB", "Call"),))]


def test_an_action_that_ends_the_hand_has_no_decision_behind_it(folder):
    provider = provider_for(folder)

    assert provider.children(Node("BB", [("SB", "Raise")])) == [Node("SB", (("SB", "Raise75"), ("BB", "Call")))]


def test_a_line_that_names_the_folds_resolves_to_the_node_that_wrote_them(tmp_path):
    """Two spellings of one decision are one node: that is what makes identity keyable."""
    write_table(
        tmp_path,
        rows=[("BU Fold;SB Raise75", "BB", "AhKs4h3s", "call", "100", "0.5", "4.0")],
    )
    provider = provider_for(tmp_path)
    implicit = provider.resolve(Node("BB", [("SB", "Raise")]))
    explicit = provider.resolve(Node("BB", [("BU", "Fold"), ("SB", "Raise")]))

    assert implicit == explicit
    assert implicit is not None and implicit.hero == "BB"
    assert provider.strategy(Node("BB", [("SB", "Raise")]), "AhKs4h3s")[0].frequency == 1.0


def test_two_rows_of_one_hand_key_are_read_as_one_strategy(tmp_path):
    write_table(
        tmp_path,
        rows=[
            ("", "SB", "AhKs4h3s", "call", "60", "1.0", "1"),
            ("", "SB", "3hKh4sAs", "call", "40", "2.0", "1"),
        ],
    )

    results = provider_for(tmp_path).strategy(Node("SB", []), "AhKs4h3s")

    assert len(results) == 1
    assert results[0].frequency == pytest.approx(0.5)
    assert results[0].ev == pytest.approx(150.0), "the EVs were in big blinds"


def test_a_table_without_ev_still_answers_but_cannot_be_graded(tmp_path):
    write_table(tmp_path, rows=[("", "SB", "AhKs4h3s", "call", "100", "", "1")])
    provider = provider_for(tmp_path)

    results = provider.strategy(Node("SB", []), "AhKs4h3s")

    assert results[0].frequency == 1.0
    assert results[0].ev is None


def test_a_table_written_in_another_seat_name_is_read_under_its_own(tmp_path):
    """A table that says ``BTN`` is a table: its own names are believed, not renamed."""
    write_table(tmp_path, rows=[("", "BTN", "AhKs4h3s", "raise 75%", "100", "1.0", "1.5")])

    provider = provider_for(tmp_path)

    assert provider.metadata().seats == ("BTN",)
    assert provider.metadata().num_players == 1
    assert provider.resolve(Node("BTN", [])) == Node("BTN", ())
    assert provider.resolve(Node("BU", [])) is None


def test_a_tree_entry_declaring_a_table_is_read_by_the_table_adapter(folder):
    """One factory, two sources: nothing downstream knows which it is looking at."""
    from preflop_advisor.strategy import provider_for as factory

    provider = factory(tree_of(folder), READER)

    assert isinstance(provider, CsvStrategyProvider)
    assert provider.metadata().seats == ("SB", "BB")


def test_a_folder_of_tables_is_detected_as_one_without_a_declaration(folder):
    """A declaration is not required: the folder answers what it holds."""
    from preflop_advisor.tree_selector import columns_of, kind_of

    assert kind_of("Table1", {"Table1.folder": str(folder)}) == "csv"
    assert kind_of("Table1", {"Table1.folder": str(folder), "Table1.kind": "Csv"}) == "csv"
    assert kind_of("Table1", {"Table1.folder": "ranges/HU-100bb-with-limp"}) == "monker"
    assert kind_of("Table1", {"Table1.folder": str(folder), "Table1.kind": "spreadsheet"}) == "csv"
    assert columns_of("Table1", {"Table1.column.action": "Decision", "Table1.column.nonsense": "x"}) == {
        "action": "Decision"
    }


def test_a_missing_folder_is_an_error_rather_than_an_empty_simulation(tmp_path):
    tree = {"plrs": 2, "bb": 100, "game": "PLO", "folder": str(tmp_path / "gone"), "kind": "csv"}

    with pytest.raises(RangeFolderNotFound):
        CsvStrategyProvider(tree, READER)


# --------------------------------------------------------------------------------------
# Importing it


def test_the_wizard_reads_a_folder_of_tables(folder):
    scan = scan_csv_simulation(str(folder), READER)

    assert scan.kind == "csv"
    assert scan.export == "CSV strategy table"
    assert scan.players == 2
    assert scan.seats == ("SB", "BB")
    assert scan.game == "PLO"
    assert scan.nodes == 4
    assert scan.range_files == 1
    assert scan.has_ev is True
    assert scan.columns["action"] == "Action"
    assert scan.problems == ()


def test_the_wizard_says_which_columns_it_read(folder):
    scan = scan_csv_simulation(str(folder), READER)

    described = scan.columns_described()
    assert "line: Line" in described
    assert "ev: EV (bb)" in described
    assert "Simulation:" in scan.summary()


def test_the_wizard_reports_the_rows_it_could_not_read(folder):
    write_table(folder, rows=[*TABLE, ("", "SB", "AhKs4h3s", "", "10", "0", "1")])

    scan = scan_csv_simulation(str(folder), READER)

    assert any("cannot be read" in note or "no action" in note for note in scan.problems)


def test_the_advisor_grid_is_read_from_a_table(folder):
    """The grid goes through the provider it always did, so a table is drawn like an export."""
    from preflop_advisor.tree_reader import TreeReader

    reader = TreeReader("AhKs4h3s", "BB", tree_of(folder), READER)

    grid = reader.get_results()
    column = [cell.get("Text") for cell in grid[0]].index("vs SB")
    drawn = {str(entry[0]): entry for entry in (grid[1][column].get("Results") or [])}

    assert set(drawn) == {"Raise2.5bb", "Call"}, "the decision the small blind opened into"
    assert drawn["Call"][1] == pytest.approx(0.6)
    assert drawn["Call"][2] == pytest.approx(80.0), "an EV of big blinds is shown in chips"


def test_a_folder_that_is_not_a_table_is_refused_rather_than_imported(tmp_path):
    (tmp_path / "notes.txt").write_text("nothing to see")

    with pytest.raises(SimulationScanError, match="no .csv files"):
        scan_csv_simulation(str(tmp_path), READER)


def test_importing_a_table_writes_the_reader_beside_it_and_no_columns_of_its_own(folder, tmp_path):
    """What the wizard read is shown and not declared: a detection belongs to one table.

    A mapping written beside the tree is applied to every table of the folder, so storing
    one table's reading would have the others looked at for columns they do not have.
    """
    scan = scan_csv_simulation(str(folder), READER)
    config = LayeredConfig(PACKAGE_CONFIG, user_path=tmp_path / "config.ini")
    request = ImportRequest(
        folder=scan.folder,
        name=scan.name,
        game=scan.game,
        players=scan.players,
        stack_bb=str(scan.stack_bb),
        kind=scan.kind,
        columns=inferred_mapping(),
    )

    key = register_simulation(config, request)

    assert config.get("TreeInfos", key) == entry_value(request)
    assert config.get("TreeInfos", f"{key}.kind") == "csv"
    assert config.get("TreeInfos", f"{key}.column.action") in (None, "")
    assert scan.columns["action"] == "Action", "the wizard still says what it read"


def test_a_mapping_the_user_corrects_is_still_written_beside_the_tree(tmp_path):
    """A correction is an instruction about the folder, which is what a declaration is."""
    write_table(tmp_path, rows=TABLE, header=["Move" if name == "Action" else name for name in HEADER])
    config = LayeredConfig(PACKAGE_CONFIG, user_path=tmp_path / "config.ini")
    request = ImportRequest(
        folder=str(tmp_path),
        name="corrected",
        game="PLO",
        players=2,
        stack_bb="100",
        kind="csv",
        columns=inferred_mapping({"action": "Move"}),
    )

    key = register_simulation(config, request)

    assert config.get("TreeInfos", f"{key}.column.action") == "Move"
    assert config.get("TreeInfos", f"{key}.column.hero") in (None, "")


def test_importing_a_range_folder_declares_no_kind_and_no_columns(tmp_path):
    config = LayeredConfig(PACKAGE_CONFIG, user_path=tmp_path / "config.ini")
    request = ImportRequest(
        folder=os.path.join(PACKAGE_ROOT, "..", "ranges", "HU-100bb-with-limp"),
        name="HU-100bb-with-limp",
        game="PLO",
        players=2,
        stack_bb="100",
    )

    key = register_simulation(config, request)

    assert config.get("TreeInfos", f"{key}.kind") in (None, "")
    assert config.get("TreeInfos", f"{key}.column.action") in (None, "")
