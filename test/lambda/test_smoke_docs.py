"""Smoke tests for documentation files.

Verifies that all required documentation files exist in the ``docs/``
directory, that Markdown files are non-empty and contain at least one
heading, and that the ``docs/index.html`` landing page contains
navigation links to each documentation file.

Requirements: 21.1, 21.3
"""

import os
import re

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DOCS_ROOT = os.path.join(_REPO_ROOT, "docs")

# All required Markdown documentation files (relative to docs/)
_REQUIRED_MD_FILES = [
    "admin/deploying-foundation.md",
    "admin/user-management.md",
    "admin/project-management.md",
    "project-admin/project-management.md",
    "project-admin/cluster-management.md",
    "user/accessing-clusters.md",
    "user/data-management.md",
    "api/reference.md",
]


def _read_file(relative_path: str) -> str:
    """Read a file under the docs directory and return its content."""
    with open(os.path.join(_DOCS_ROOT, relative_path), "r") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Tests — Markdown documentation files
# ---------------------------------------------------------------------------

class TestMarkdownDocsExist:
    """Verify all required Markdown documentation files exist, are non-empty,
    and contain at least one Markdown heading.

    Validates: Requirements 21.1, 21.3
    """

    @pytest.mark.parametrize("doc_path", _REQUIRED_MD_FILES)
    def test_file_exists(self, doc_path: str):
        full_path = os.path.join(_DOCS_ROOT, doc_path)
        assert os.path.isfile(full_path), f"Missing documentation file: docs/{doc_path}"

    @pytest.mark.parametrize("doc_path", _REQUIRED_MD_FILES)
    def test_file_is_non_empty(self, doc_path: str):
        content = _read_file(doc_path)
        assert len(content.strip()) > 0, f"Documentation file is empty: docs/{doc_path}"

    @pytest.mark.parametrize("doc_path", _REQUIRED_MD_FILES)
    def test_file_contains_markdown_heading(self, doc_path: str):
        content = _read_file(doc_path)
        assert re.search(r"^#{1,6}\s+\S", content, re.MULTILINE), (
            f"Documentation file has no Markdown heading: docs/{doc_path}"
        )


# ---------------------------------------------------------------------------
# Tests — index.html landing page
# ---------------------------------------------------------------------------

class TestIndexHtml:
    """Verify the docs/index.html landing page exists and contains navigation
    links to all documentation files.

    Validates: Requirements 21.1, 21.3
    """

    @pytest.fixture(scope="class")
    def index_content(self) -> str:
        path = os.path.join(_DOCS_ROOT, "index.html")
        assert os.path.isfile(path), "Missing documentation landing page: docs/index.html"
        with open(path, "r") as fh:
            return fh.read()

    def test_index_is_non_empty(self, index_content: str):
        assert len(index_content.strip()) > 0, "docs/index.html is empty"

    @pytest.mark.parametrize("doc_path", _REQUIRED_MD_FILES)
    def test_index_has_nav_link(self, index_content: str, doc_path: str):
        """The landing page should reference each doc file via a data-doc
        attribute or an <a> href."""
        has_data_doc = f'data-doc="{doc_path}"' in index_content
        has_href = f'href="{doc_path}"' in index_content
        assert has_data_doc or has_href, (
            f"docs/index.html has no navigation link for {doc_path}"
        )
