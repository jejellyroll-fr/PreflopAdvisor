import os
from configparser import ConfigParser

import pytest
from PySide6.QtWidgets import QApplication


# Ensure QApplication instance exists
@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def configs():
    config = ConfigParser()
    config_path = os.path.join(os.path.dirname(__file__), "..", "preflop_advisor", "config.ini")
    config.read(config_path)
    return config


def test_card_selector(qapp, configs):
    from preflop_advisor.card_selector import CardSelector
    
    selected_hands = []
    def callback():
        selected_hands.append(cs.get_selected_hand())

    cs = CardSelector(configs["CardSelector"], callback)
    
    # Test setting num cards
    cs.set_num_cards(4)
    assert cs.num_cards == 4
    
    # Click 4 buttons (Ah, Kh, Qh, Jh) -> column 0 is 'h'
    # row 0='A', row 1='K', row 2='Q', row 3='J'
    cs.process_button_clicked(0, 0)
    cs.process_button_clicked(1, 0)
    cs.process_button_clicked(2, 0)
    cs.process_button_clicked(3, 0)
    
    assert len(cs.selected_cards) == 4
    assert cs.get_selected_hand() == "AhKhQhJh"
    assert cs.get_hand() == "AhKhQhJh"
    # Test signal emission
    signal_received = []
    cs.handChanged.connect(lambda h: signal_received.append(h))
    cs.set_num_cards(2)
    cs.process_button_clicked(0, 0)
    cs.process_button_clicked(1, 0)
    assert len(signal_received) == 1
    assert signal_received[0] == "AhKh"


def test_position_selector(qapp, configs):
    from preflop_advisor.position_selector import PositionSelector
    
    positions = []
    def callback():
        positions.append(ps.get_position())

    ps = PositionSelector(None, configs["PositionSelector"], callback)
    
    # Test dynamic active positions update
    ps.update_active_positions(2)
    assert ps.get_position() in ps.position_list

    ps.update_active_positions(6)
    assert ps.get_position() in ps.position_list


def test_tree_selector(qapp, configs):
    from preflop_advisor.tree_selector import TreeSelector
    
    tree_changed_count = []
    def callback():
        tree_changed_count.append(1)

    ts = TreeSelector(
        None,
        configs["TreeSelector"],
        configs["TreeInfos"],
        configs["TreeToolTips"] if "TreeToolTips" in configs else {},
        callback
    )
    
    info = ts.get_tree_infos()
    assert info is not None
    assert "game" in info
    assert "plrs" in info


def test_main_window(qapp):
    from preflop_advisor.gui import MainWindow
    
    window = MainWindow()
    assert window is not None
    
    # Simulate selecting hand
    window.card_selector.set_num_cards(4)
    window.card_selector.process_button_clicked(0, 0)  # Ah
    window.card_selector.process_button_clicked(1, 0)  # Kh
    window.card_selector.process_button_clicked(2, 0)  # Qh
    window.card_selector.process_button_clicked(3, 0)  # Jh
    
    # Trigger update
    window.update_output_frame()
    
    assert window.output is not None
