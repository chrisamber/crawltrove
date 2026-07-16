import pathlib

import pytest


@pytest.fixture
def tmp_corpus(tmp_path) -> pathlib.Path:
    """A fresh DATA_DIR-style root for corpus/metadata writes."""
    return tmp_path
