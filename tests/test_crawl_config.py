import pytest
from pydantic import ValidationError

from app.crawl.config import CrawlConfig


def test_crawl_defaults_are_bounded():
    config = CrawlConfig(url="https://example.com")
    assert config.limit == 10
    assert config.maxDepth == 3
    assert config.maxBytes == 1024**3
    assert config.maxArtifactBytes == 2 * 1024**3
    assert config.timeoutSeconds == 21600
    assert config.acquisition.maxAttempts == 4


def test_caller_cannot_exceed_server_caps():
    with pytest.raises(ValidationError):
        CrawlConfig(url="https://example.com", limit=101)
    with pytest.raises(ValidationError):
        CrawlConfig(url="https://example.com", maxDepth=6)


def test_nested_acquisition_config_is_immutable():
    config = CrawlConfig(url="https://example.com")

    with pytest.raises(ValidationError):
        config.acquisition.maxAttempts = 2


def test_provider_budgets_use_native_meter_objects():
    config = CrawlConfig.model_validate({
        "url": "https://example.com",
        "acquisition": {
            "provider": "auto",
            "creditBudgets": {
                "firecrawl": {"credits": 5},
                "brightdata": {"requests": 2},
                "browserbase": {"browserMinutes": 3, "proxyBytes": 1000000},
            },
        },
    })
    assert config.acquisition.creditBudgets.firecrawl.credits == 5
    assert config.acquisition.creditBudgets.browserbase.proxyBytes == 1000000
