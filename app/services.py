"""Shared process singletons for scrape / legacy-crawl / research / batch.

Centralising these here lets main.py, runner.py, and scheduler.py share one
WebScraper (and related managers) without importing main.py (import cycle).

``crawler`` is the **legacy** in-memory ``WebCrawler`` only. Durable crawls use
``app.crawl.service.crawl_service`` / ``submit_crawl``. Research and batch keep
their own in-memory job stores for 202+poll visibility.
"""
from app.batch import BatchManager
from app.crawler import WebCrawler
from app.research import ResearchManager
from app.scraper import WebScraper

scraper = WebScraper()
crawler = WebCrawler()  # legacy; prefer app.crawl for new crawl work
researcher = ResearchManager()
batcher = BatchManager()
