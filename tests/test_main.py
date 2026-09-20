from mcp_sidekit import main


def test_main_runs(capsys):
    main()
    assert capsys.readouterr().out == "mcp-sidekit\n"
