from mcp_portal.configure.interaction import ScriptedPrompter


def test_scripted_prompter_replays_confirm_answers_in_order():
    prompter = ScriptedPrompter(confirms=[True, False], texts=[])
    assert prompter.confirm("expose billing.*?", default=True) is True
    assert prompter.confirm("expose internal.*?", default=True) is False


def test_scripted_prompter_returns_default_when_confirms_exhausted():
    prompter = ScriptedPrompter(confirms=[], texts=[])
    assert prompter.confirm("expose billing.*?", default=False) is False
    assert prompter.confirm("expose billing.*?", default=True) is True


def test_scripted_prompter_replays_text_answers_and_falls_back_to_default():
    prompter = ScriptedPrompter(confirms=[], texts=["payment_initiation"])
    assert prompter.text("required detail type?", default="") == "payment_initiation"
    assert prompter.text("required detail type?", default="fallback") == "fallback"
