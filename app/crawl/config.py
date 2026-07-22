from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


Provider = Literal["auto", "local", "firecrawl", "brightdata", "browserbase"]


class FirecrawlBudget(BaseModel):
    model_config = ConfigDict(frozen=True)

    credits: float = Field(ge=0)


class BrightDataBudget(BaseModel):
    model_config = ConfigDict(frozen=True)

    requests: int = Field(ge=0)


class BrowserbaseBudget(BaseModel):
    model_config = ConfigDict(frozen=True)

    browserMinutes: float = Field(ge=0)
    proxyBytes: int = Field(ge=0)


class CreditBudgets(BaseModel):
    model_config = ConfigDict(frozen=True)

    firecrawl: Optional[FirecrawlBudget] = None
    brightdata: Optional[BrightDataBudget] = None
    browserbase: Optional[BrowserbaseBudget] = None


class AcquisitionConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: Provider = "auto"
    maxAttempts: int = Field(4, ge=1, le=4)
    creditBudgets: CreditBudgets = Field(default_factory=CreditBudgets)
    sessionProfile: Optional[str] = Field(None, min_length=1, max_length=128)
    allowHumanIntervention: bool = False


class CrawlConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, frozen=True)

    url: str
    limit: int = Field(10, ge=1, le=100)
    maxDepth: int = Field(3, ge=0, le=5)
    onlyMainContent: bool = True
    engine: Literal["auto", "http", "browser"] = "auto"
    useSitemap: bool = True
    screenshots: bool = False
    screenshotMaxWidth: int = Field(16384, ge=1, le=16384)
    screenshotMaxHeight: int = Field(16384, ge=1, le=16384)
    respectRobots: bool = True
    robotsFailOpen: bool = False
    minDelayMs: int = Field(1000, ge=0, le=60000)
    maxBrowserPages: int = Field(25, ge=0, le=100)
    maxOrigins: int = Field(1, ge=1, le=100)
    maxFailures: int = Field(100, ge=1, le=100)
    maxBytes: int = Field(1024**3, ge=1, le=1024**3)
    maxArtifactBytes: int = Field(2 * 1024**3, ge=1, le=2 * 1024**3)
    timeoutSeconds: int = Field(21600, ge=1, le=21600)
    acquisition: AcquisitionConfig = Field(default_factory=AcquisitionConfig)
