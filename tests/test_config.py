"""Tests for AppConfig and configuration management."""

import pytest
from tests.conftest import AppConfig


class TestAppConfig:
    """Test suite for AppConfig."""

    def test_config_defaults(self, base_config: AppConfig):
        """Test that base_config fixture provides correct defaults."""
        assert base_config.dry_run is True
        assert base_config.notional == 25.0
        assert base_config.db_path == ":memory:"
        assert base_config.log_level == "DEBUG"

    def test_config_with_custom_values(self):
        """Test AppConfig with custom values."""
        config = AppConfig(
            dry_run=False,
            notional=50.0,
            exchange_name="production",
            api_key="real_key",
            api_secret="real_secret",
            db_path="/tmp/cerberus.db",
            log_level="INFO"
        )
        assert config.dry_run is False
        assert config.notional == 50.0
        assert config.exchange_name == "production"
        assert config.db_path == "/tmp/cerberus.db"
