# -*- coding: utf-8 -*-
"""History manager for SOPHIA Climate decisions.

Stores climate decisions in a JSON file (recent + rolling disk cache) and,
when a sophia_core RAG backend is available, also mirrors each decision
into the sophia_climate_decisions Qdrant collection for long-horizon
analysis. All Qdrant/TEI I/O is delegated to sophia_core's public RAG
API; this module never talks to Qdrant or TEI directly.
"""
import json
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from collections import deque
from typing import Any, Dict, List, Optional
import logging

from .const import (
    RAG_COLLECTION_DECISIONS,
    DEFAULT_RAG_RETENTION_DAYS,
    DEFAULT_RAG_MEMORY_ENTRIES,
)

_LOGGER = logging.getLogger(__name__)


class ClimateHistoryManager:
    """Manages climate decision history with file-based storage and optional RAG mirroring"""

    def __init__(
        self,
        hass,
        config_dir: str,
        max_memory_entries: int = DEFAULT_RAG_MEMORY_ENTRIES,
        max_file_entries: int = 500,
        llm_client=None,
        rag_enabled: bool = False,
        rag_collection: str = RAG_COLLECTION_DECISIONS,
        rag_retention_days: int = DEFAULT_RAG_RETENTION_DAYS,
    ):
        """Initialize history manager

        Args:
            hass: Home Assistant instance
            config_dir: Path to HA config directory
            max_memory_entries: Number of recent decisions to keep in memory (for sensor display)
            max_file_entries: Total decisions to keep on disk
            llm_client: sophia_core SophiaLLMClient for RAG I/O. Required when rag_enabled.
            rag_enabled: When True, mirror each decision into the configured Qdrant collection.
            rag_collection: Qdrant collection name for climate decisions.
            rag_retention_days: Maximum age (in days) to retain decisions in RAG before purging.
        """
        self.hass = hass
        self.history_file = Path(config_dir) / "custom_components" / "sophia_climate" / "climate_decisions.json"
        self.max_memory_entries = max_memory_entries
        self.max_file_entries = max_file_entries

        # RAG settings - all Qdrant/TEI I/O flows through llm_client (sophia_core)
        self._llm = llm_client
        self._rag_enabled = bool(rag_enabled and llm_client is not None)
        self._rag_collection = rag_collection
        self._rag_retention_days = max(1, int(rag_retention_days))

        # In-memory cache for quick access (displayed in sensor attributes)
        self.memory_history = deque(maxlen=max_memory_entries)

        # Statistics cache
        self._stats_cache = None
        self._stats_cache_time = None
        self._stats_cache_duration = 60  # Cache stats for 60 seconds

        _LOGGER.info(
            "Initialized ClimateHistoryManager (file=%s, rag_enabled=%s, collection=%s, retention=%sd)",
            self.history_file, self._rag_enabled, self._rag_collection, self._rag_retention_days,
        )

    async def initialize(self):
        """Initialize manager - load existing history and ensure RAG collection exists"""
        await self._load_memory_history()
        if self._rag_enabled:
            try:
                ok = await self._llm.rag_ensure_collection(self._rag_collection)
                if not ok:
                    _LOGGER.warning(
                        "ClimateHistoryManager: rag_ensure_collection returned False for '%s' - "
                        "RAG mirroring will be attempted per-write but may fail",
                        self._rag_collection,
                    )
            except Exception as err:
                _LOGGER.warning(
                    "ClimateHistoryManager: failed to ensure RAG collection '%s': %s",
                    self._rag_collection, err,
                )

    async def add_decision(self, decision_data: Dict[str, Any]) -> None:
        """Add a new climate decision to history

        Args:
            decision_data: Dictionary containing decision details
        """
        # Add timestamp if not present
        if "timestamp" not in decision_data:
            decision_data["timestamp"] = datetime.now().isoformat()

        # Add to memory cache (for sensor display)
        self.memory_history.appendleft(decision_data)

        # Append to file (async)
        await self._append_to_file(decision_data)

        # Mirror into RAG (best-effort; never raises back to caller)
        if self._rag_enabled:
            try:
                await self._store_to_rag(decision_data)
            except Exception as err:
                _LOGGER.debug(
                    "ClimateHistoryManager: RAG mirror skipped (%s): %s",
                    decision_data.get("zone"), err,
                )

        # Invalidate stats cache
        self._stats_cache = None

        _LOGGER.debug(f"Added decision: {decision_data.get('decision')} for {decision_data.get('zone')}")

    async def _store_to_rag(self, decision_data: Dict[str, Any]) -> bool:
        """Upsert a decision into the RAG collection via sophia_core."""
        if not self._llm:
            return False

        zone = decision_data.get("zone", "unknown")
        decision = decision_data.get("decision", "NO_CHANGE")
        reasoning = decision_data.get("reasoning", "")
        indoor = decision_data.get("indoor_temp")
        target = decision_data.get("target_temp")
        outdoor = decision_data.get("outdoor_temp")
        delta = decision_data.get("temp_delta")
        season = decision_data.get("season", "unknown")
        is_sleep = decision_data.get("is_sleep_time", False)
        hvac_mode = decision_data.get("hvac_mode", "unknown")
        hvac_action = decision_data.get("hvac_action", "unknown")
        ts_iso = decision_data.get("timestamp", datetime.now().isoformat())

        # Parse timestamp for natural-language day/time context (same style as presence)
        try:
            dt = datetime.fromisoformat(ts_iso)
            day_str = dt.strftime("%A")
            time_str = dt.strftime("%I:%M %p")
        except (ValueError, TypeError):
            day_str = "unknown day"
            time_str = "unknown time"

        def _fmt(value, suffix=""):
            if value is None:
                return "n/a"
            return f"{value}{suffix}"

        text = (
            f"[{ts_iso}] Climate decision for zone {zone} on {day_str} at {time_str}: {decision}. "
            f"Indoor {_fmt(indoor, 'F')}, target {_fmt(target, 'F')}, "
            f"outdoor {_fmt(outdoor, 'F')}, delta {_fmt(delta, 'F')}. "
            f"Season: {season}, sleep window: {bool(is_sleep)}. "
            f"HVAC mode: {hvac_mode}, action: {hvac_action}. "
            f"Reasoning: {reasoning}"
        )

        metadata = {
            "type": "climate_decision",
            "zone": zone,
            "decision": decision,
            "reasoning": reasoning,
            "indoor_temp": indoor,
            "target_temp": target,
            "outdoor_temp": outdoor,
            "temp_delta": delta,
            "season": season,
            "is_sleep_time": bool(is_sleep),
            "hvac_mode": hvac_mode,
            "hvac_action": hvac_action,
            "day_of_week": day_str,
            "time": time_str,
            "timestamp": ts_iso,
        }

        # Deterministic doc_id using full millisecond precision to prevent
        # collision between decisions in the same second for the same zone.
        safe_ts = ts_iso[:23].replace(":", "-").replace(" ", "T").replace(".", "-")
        doc_id = f"decision_{zone}_{safe_ts}"

        return await self._llm.rag_upsert(
            self._rag_collection, text, metadata, doc_id
        )

    async def purge_rag_older_than(self, days: Optional[int] = None) -> int:
        """Delete RAG decisions older than the retention window (default: configured retention)."""
        if not self._rag_enabled or not self._llm:
            return 0
        effective = int(days) if days is not None else self._rag_retention_days
        cutoff_iso = (datetime.now() - timedelta(days=effective)).isoformat()
        try:
            return await self._llm.rag_purge_older_than(
                self._rag_collection, cutoff_iso, timestamp_field="timestamp"
            )
        except Exception as err:
            _LOGGER.warning(
                "ClimateHistoryManager: RAG purge failed for '%s': %s",
                self._rag_collection, err,
            )
            return 0
    
    async def _append_to_file(self, entry: Dict[str, Any]) -> None:
        """Append a single entry to the JSON file with rotation"""
        try:
            # Read existing history
            history = await self._read_file()
            
            # Add new entry at the beginning (newest first)
            history.insert(0, entry)
            
            # Rotate if needed
            if len(history) > self.max_file_entries:
                history = history[:self.max_file_entries]
                _LOGGER.info(f"Rotated history file, kept {self.max_file_entries} most recent entries")
            
            # Write back
            await self._write_file(history)
            
        except Exception as e:
            _LOGGER.error(f"Error appending to history file: {e}")
    
    async def _read_file(self) -> List[Dict[str, Any]]:
        """Read history from file"""
        if not self.history_file.exists():
            return []
        
        def _read():
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                _LOGGER.warning("History file corrupted, starting fresh")
                return []
            except Exception as e:
                _LOGGER.error(f"Error reading history file: {e}")
                return []

        return await self.hass.async_add_executor_job(_read)

    async def _write_file(self, history: List[Dict[str, Any]]) -> None:
        """Write history to file"""
        self.history_file.parent.mkdir(parents=True, exist_ok=True)

        def _write():
            try:
                temp_file = self.history_file.with_suffix('.tmp')
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(history, f, indent=2, ensure_ascii=False)
                temp_file.replace(self.history_file)
            except Exception as e:
                _LOGGER.error(f"Error writing history file: {e}")
                raise

        await self.hass.async_add_executor_job(_write)

    async def _load_memory_history(self) -> None:
        """Load recent history into memory cache"""
        history = await self._read_file()
        for entry in history[:self.max_memory_entries]:
            self.memory_history.append(entry)
        _LOGGER.info(f"Loaded {len(self.memory_history)} recent decisions into memory")

    def get_memory_history(self) -> List[Dict[str, Any]]:
        """Get recent history from memory (for sensor attributes)"""
        return list(self.memory_history)

    async def get_full_history(self, days: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get full history from file, optionally filtered by days"""
        history = await self._read_file()
        if days:
            cutoff = datetime.now() - timedelta(days=days)
            history = [
                h for h in history
                if datetime.fromisoformat(h['timestamp']) > cutoff
            ]
        return history

    async def get_statistics(self, use_cache: bool = True) -> Dict[str, Any]:
        """Calculate statistics from history"""
        if use_cache and self._stats_cache and self._stats_cache_time:
            age = (datetime.now() - self._stats_cache_time).total_seconds()
            if age < self._stats_cache_duration:
                return self._stats_cache

        history = await self._read_file()
        total = len(history)
        if total == 0:
            return {
                "total_decisions": 0,
                "action_decisions": 0,
                "no_change_decisions": 0,
                "action_percentage": 0,
                "stability_percentage": 0,
            }

        action_decisions = sum(
            1 for d in history
            if d.get("decision") != "NO_CHANGE"
        )
        no_change_decisions = total - action_decisions
        stats = {
            "total_decisions": total,
            "action_decisions": action_decisions,
            "no_change_decisions": no_change_decisions,
            "action_percentage": round((action_decisions / total * 100), 1),
            "stability_percentage": round((no_change_decisions / total * 100), 1),
        }
        self._stats_cache = stats
        self._stats_cache_time = datetime.now()
        return stats

    async def cleanup_old(self, days: int = 30) -> int:
        """Remove entries older than X days"""
        cutoff = datetime.now() - timedelta(days=days)
        history = await self._read_file()
        filtered = [
            h for h in history
            if datetime.fromisoformat(h['timestamp']) > cutoff
        ]
        removed = len(history) - len(filtered)
        if removed > 0:
            await self._write_file(filtered)
            _LOGGER.info(f"Cleaned up {removed} old history entries (older than {days} days)")
        return removed

    async def get_latest_decision(self) -> Optional[Dict[str, Any]]:
        """Get the most recent decision"""
        if self.memory_history:
            return self.memory_history[0]
        history = await self._read_file()
        return history[0] if history else None

    async def export_to_csv(self, filepath: str) -> None:
        """Export history to CSV file for analysis"""
        import csv
        history = await self._read_file()
        if not history:
            _LOGGER.warning("No history to export")
            return

        def _write_csv():
            with open(filepath, 'w', newline='', encoding='utf-8') as f:
                fieldnames = list(history[0].keys())
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for entry in history:
                    writer.writerow(entry)

        await self.hass.async_add_executor_job(_write_csv)
        _LOGGER.info(f"Exported {len(history)} decisions to {filepath}")
