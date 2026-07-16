"""Shared singletons for the scraper, crawler, and research manager.

Centralising these here lets main.py (HTTP endpoints), runner.py (job execution)
and scheduler.py all share one WebScraper/WebCrawler instance without importing
main.py (which would create an import cycle). The crawler and research manager
keep their in-memory job stores, so background runs stay visible via their GET
endpoints.
"""
from app.batch import BatchManager
from app.crawler import WebCrawler
from app.research import ResearchManager
from app.scraper import WebScraper

scraper = WebScraper()
crawler = WebCrawler()
researcher = ResearchManager()
batcher = BatchManager()
