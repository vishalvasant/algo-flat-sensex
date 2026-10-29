from algoflat.notifications.policy import is_important_alert


def test_trade_and_kill_switch_always_important():
    assert is_important_alert("trade", "Anything")
    assert is_important_alert("kill_switch", "Anything")


def test_system_titles_whitelist():
    assert is_important_alert("system", "Auto trading ON")
    assert is_important_alert("system", "Paper account reset")
    assert not is_important_alert("system", "Candle refresh complete")


def test_unknown_type_uses_title_whitelist():
    assert is_important_alert("other", "Trade entry")
    assert not is_important_alert("other", "Heartbeat")
