"""Main-content extraction via trafilatura.

Trafilatura (with favor_precision) is the extractor used by the FineWeb and
RefinedWeb pretraining pipelines — it beat readability/jusText on both human
inspection and downstream model quality. We use it as the primary markdown
extractor and fall back to the BeautifulSoup+markdownify cleaner when it
returns nothing usable (very short or empty extraction).
"""
from typing import Any, Dict, Optional

import trafilatura

# Below this many characters, assume trafilatura choked and fall back
MIN_EXTRACT_CHARS = 200


def extract(html: str, url: str, favor_precision: bool = True) -> Optional[Dict[str, Any]]:
    """Extract main content as markdown plus document metadata.

    Returns None when extraction fails or is implausibly short, signalling
    the caller to fall back to the legacy cleaner.
    """
    try:
        markdown = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_links=True,
            include_images=True,
            include_tables=True,
            favor_precision=favor_precision,
        )
    except Exception:
        return None
    if not markdown or len(markdown) < MIN_EXTRACT_CHARS:
        return None

    meta: Dict[str, Any] = {}
    try:
        doc = trafilatura.extract_metadata(html, default_url=url)
        if doc:
            meta = {
                "title": doc.title or "",
                "description": doc.description or "",
                "author": doc.author or "",
                "date": doc.date or "",
                "sitename": doc.sitename or "",
                "license": doc.license or "",
            }
    except Exception:
        pass

    return {"markdown": markdown.strip(), "meta": meta}
