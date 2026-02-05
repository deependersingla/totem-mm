import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _convert_datetime_to_iso(obj: Any) -> Any:
    """
    Recursively convert datetime objects to ISO format strings.
    
    Args:
        obj: Object that may contain datetime objects
        
    Returns:
        Object with datetime objects converted to ISO strings
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {key: _convert_datetime_to_iso(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [_convert_datetime_to_iso(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(_convert_datetime_to_iso(item) for item in obj)
    else:
        return obj


class PriceDataWriter:
    """Handles writing price data to files with rotation and formatting."""
    
    def __init__(
        self,
        data_dir: str = "data",
        market_id: str = "unknown",
        max_file_size_mb: int = 100,
        flush_interval: int = 10,
    ):
        """
        Initialize price data writer.
        
        Args:
            data_dir: Directory to store data files
            market_id: Market ID for file naming
            max_file_size_mb: Maximum file size before rotation (MB)
            flush_interval: Number of writes before flushing to disk
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        self.market_id = market_id
        self.max_file_size = max_file_size_mb * 1024 * 1024
        self.flush_interval = flush_interval
        self.write_count = 0
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.current_file = self.data_dir / f"prices_{market_id}_{timestamp}.jsonl"
        
        logger.info("Price data writer initialized: %s", self.current_file)
    
    def write_price_update(
        self,
        market_id: str,
        odds: Dict[int, Dict[str, Any]],
        timestamp: Optional[datetime] = None,
    ) -> None:
        """
        Write a price update to the data file.
        
        Args:
            market_id: Market ID
            odds: Dictionary of odds data keyed by selection ID
            timestamp: Timestamp of the update (default: current time)
        """
        if timestamp is None:
            timestamp = datetime.now()
        
        record = {
            "timestamp": timestamp.isoformat(),
            "market_id": market_id,
            "odds": _convert_datetime_to_iso(odds),
        }
        
        try:
            if self._should_rotate():
                self._rotate_file()
            
            with open(self.current_file, "a", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False)
                f.write("\n")
            
            self.write_count += 1
            
            if self.write_count % self.flush_interval == 0:
                self._flush()
                
        except Exception as e:
            logger.error("Error writing price data: %s", e, exc_info=True)
    
    def _should_rotate(self) -> bool:
        """Check if file rotation is needed."""
        if not self.current_file.exists():
            return False
        return self.current_file.stat().st_size >= self.max_file_size
    
    def _rotate_file(self) -> None:
        """Rotate to a new file."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        old_file = self.current_file
        self.current_file = self.data_dir / f"prices_{self.market_id}_{timestamp}.jsonl"
        logger.info("Rotating data file: %s -> %s", old_file.name, self.current_file.name)
    
    def _flush(self) -> None:
        """Flush any buffered data (handled by file system, but kept for future use)."""
        pass
    
    def write_snapshot(
        self,
        market_id: str,
        odds: Dict[int, Dict[str, Any]],
        filename: Optional[str] = None,
    ) -> Path:
        """
        Write a complete snapshot of current odds to a JSON file.
        
        Args:
            market_id: Market ID
            odds: Dictionary of odds data
            filename: Optional custom filename (default: snapshot_{market_id}_{timestamp}.json)
            
        Returns:
            Path to the written file
        """
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"snapshot_{market_id}_{timestamp}.json"
        
        snapshot_file = self.data_dir / filename
        
        snapshot = {
            "timestamp": datetime.now().isoformat(),
            "market_id": market_id,
            "odds": _convert_datetime_to_iso(odds),
        }
        
        try:
            with open(snapshot_file, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=2, ensure_ascii=False)
            
            logger.info("Snapshot written: %s", snapshot_file)
            return snapshot_file
        except Exception as e:
            logger.error("Error writing snapshot: %s", e, exc_info=True)
            raise
