"""Tests for CerberusStorage and database operations."""

import pytest
from pathlib import Path
from tests.conftest import CerberusStorage


class TestCerberusStorage:
    """Test suite for CerberusStorage."""

    def test_storage_initialization(self, tmp_db: CerberusStorage):
        """Test that tmp_db fixture initializes storage correctly."""
        assert tmp_db.db_path is not None
        assert tmp_db._initialized is True

    def test_storage_with_memory_db(self):
        """Test CerberusStorage with in-memory SQLite database."""
        storage = CerberusStorage(":memory:")
        storage.initialize()
        assert storage._initialized is True
        storage.close()

    def test_storage_with_file_path(self, tmp_path: Path):
        """Test CerberusStorage with file-based database."""
        db_file = tmp_path / "test.db"
        storage = CerberusStorage(str(db_file))
        storage.initialize()
        assert storage._initialized is True
        assert storage.db_path == str(db_file)
        storage.close()

    def test_storage_context_cleanup(self, tmp_path: Path):
        """Test that storage properly cleans up resources."""
        db_file = tmp_path / "test_cleanup.db"
        storage = CerberusStorage(str(db_file))
        storage.initialize()
        storage.close()
        # Verify storage was closed without error
        assert storage._initialized is True
