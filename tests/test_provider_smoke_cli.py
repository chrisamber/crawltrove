from scripts.smoke_providers import REQUIRED_PROVIDER_ENV, main, parse_args


def test_smoke_preflight_lists_missing_keys_without_values(monkeypatch, capsys):
    for name in REQUIRED_PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)

    assert main(["--preflight"]) == 2
    output = capsys.readouterr().err
    assert "FIRECRAWL_API_KEY" in output
    assert "fc-" not in output


def test_smoke_defaults_to_one_item_and_hard_caps():
    args = parse_args([])

    assert args.url == "https://example.com"
    assert args.firecrawl_credits == 1
    assert args.brightdata_requests == 1
    assert args.browserbase_minutes == 1


def test_smoke_refuses_higher_budget_before_any_readiness_call(monkeypatch, capsys):
    for name in REQUIRED_PROVIDER_ENV:
        monkeypatch.setenv(name, "configured")

    assert main(["--preflight", "--firecrawl-credits", "2"]) == 2
    assert "one-unit release cap" in capsys.readouterr().err
