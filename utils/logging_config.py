import logging
import logging.handlers
import os
from pathlib import Path
from typing import Optional


def setup_logging(
    log_dir: str = "logs",
    log_file: str = "totem-mm.log",
    log_level: int = logging.INFO,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """
    Set up logging configuration with both console and rotating file handlers.
    
    Args:
        log_dir: Directory to store log files
        log_file: Name of the log file
        log_level: Logging level (default: INFO)
        max_bytes: Maximum size of log file before rotation (default: 10MB)
        backup_count: Number of backup log files to keep (default: 5)
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    
    log_file_path = log_path / log_file
    
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    
    if root_logger.handlers:
        root_logger.handlers.clear()
    
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    file_handler = logging.handlers.RotatingFileHandler(
        log_file_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    
    logging.info("Logging configured: console and file (%s)", log_file_path)
