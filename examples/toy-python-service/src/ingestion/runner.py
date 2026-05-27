"""Ingestion pipeline runner. Synthetic example for Anvil demonstrations."""

from ingestion.config import IngestionConfig


def run_ingestion(config: IngestionConfig) -> dict:
    """Execute the ingestion pipeline."""
    return {
        "status": "completed",
        "source": config.source_url,
        "records_processed": 0,
        "batch_size": config.batch_size,
    }
