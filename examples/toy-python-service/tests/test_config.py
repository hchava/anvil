"""Tests for ingestion config loader."""

import pytest
from ingestion.config import load_config, IngestionConfig


def test_load_config_happy_path():
    config = load_config({"source_url": "https://example.com/data"})
    assert isinstance(config, IngestionConfig)
    assert config.source_url == "https://example.com/data"
    assert config.batch_size == 100


def test_load_config_missing_source_url():
    with pytest.raises(KeyError, match="source_url"):
        load_config({})


def test_load_config_invalid_source_url():
    with pytest.raises(ValueError, match="Invalid source_url"):
        load_config({"source_url": "not-a-url"})
