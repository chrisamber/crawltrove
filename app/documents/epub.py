"""EPUB extraction with stdlib ZIP/XML parsing and permissive HTML tools."""

import io
import posixpath
import zipfile
from typing import Optional
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

import trafilatura
from bs4 import BeautifulSoup
from markdownify import markdownify

from app.documents.types import ParsedDoc

MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_FILES = 2_000
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_ENTRY_BYTES = 10 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
MAX_PAGES = 500


def _safe_archive(zf: zipfile.ZipFile) -> bool:
    infos = zf.infolist()
    if len(infos) > MAX_FILES:
        return False
    if sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_BYTES:
        return False
    for info in infos:
        if info.file_size and info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO:
            return False
    return True


def _read(zf: zipfile.ZipFile, name: str) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > MAX_ENTRY_BYTES:
        raise ValueError("oversized EPUB content entry")
    return zf.read(info)


def _path(base: str, href: str) -> str:
    path = unquote(urlsplit(href).path)
    if not path or "\\" in path or path.startswith("/"):
        raise ValueError("unsafe EPUB path")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(base), path))
    if resolved == ".." or resolved.startswith("../"):
        raise ValueError("unsafe EPUB path")
    return resolved


def _html_markdown(data: bytes) -> str:
    soup = BeautifulSoup(data, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    html = str(soup)
    markdown = trafilatura.extract(
        html,
        output_format="markdown",
        include_links=True,
        include_images=True,
        include_tables=True,
    )
    if markdown:
        return markdown.strip()
    return markdownify(str(soup), heading_style="ATX", bullets="-").strip()


def extract(data: bytes) -> Optional[ParsedDoc]:
    """Convert spine-ordered EPUB chapters to markdown, rejecting unsafe ZIPs."""
    if len(data) > MAX_ARCHIVE_BYTES:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if not _safe_archive(zf):
                return None

            container = ElementTree.fromstring(_read(zf, "META-INF/container.xml"))
            rootfile = container.find(".//{*}rootfile")
            if rootfile is None:
                return None
            opf_path = _path("", rootfile.attrib.get("full-path", ""))
            package = ElementTree.fromstring(_read(zf, opf_path))

            metadata = package.find(".//{*}metadata")
            title_node = metadata.find("{*}title") if metadata is not None else None
            title = "".join(title_node.itertext()).strip() if title_node is not None else ""

            manifest_node = package.find(".//{*}manifest")
            spine_node = package.find(".//{*}spine")
            if manifest_node is None or spine_node is None:
                return None
            manifest = {
                item.attrib.get("id"): (item.attrib.get("href", ""),
                                        item.attrib.get("media-type", ""))
                for item in manifest_node
                if item.attrib.get("id")
            }

            parts = []
            pages = 0
            for itemref in list(spine_node)[:MAX_PAGES]:
                item = manifest.get(itemref.attrib.get("idref"))
                if not item:
                    continue
                href, media_type = item
                if media_type not in {"application/xhtml+xml", "text/html"}:
                    continue
                chapter = _read(zf, _path(opf_path, href))
                parts.append(_html_markdown(chapter))
                pages += 1
    except Exception:
        return None

    markdown = "\n\n".join(part for part in parts if part).strip()
    if not markdown:
        return None
    return {
        "markdown": markdown,
        "title": title,
        "pages": pages,
        "ocr": None,
        "extractor": "epub",
    }
