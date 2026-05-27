"""Ingestion pipeline configuration loader.

This is a synthetic example service for Anvil demonstrations.
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class IngestionConfig:
    """Configuration for the ingestion pipeline."""
    source_url: str
    batch_size: int = 100
    retry_count: int = 3
    timeout_seconds: int = 30


def load_config(config_dict: dict[str, Any]) -> IngestionConfig:
    """Load ingestion config from a dictionary.

    Raises:
        KeyError: If required keys are missing.
        ValueError: If values are invalid.
    """
    if "source_url" not in config_dict:
        raise KeyError("Missing required config key: 'source_url'")

    source_url = config_dict["source_url"]
    if not isinstance(source_url, str) or not source_url.startswith("http"):
        raise ValueError(f"Invalid source_url: {source_url}")

    return IngestionConfig(
        source_url=source_url,
        batch_size=config_dict.get("batch_size", 100),
        retry_count=config_dict.get("retry_count", 3),
        timeout_seconds=config_dict.get("timeout_seconds", 30),
    )
