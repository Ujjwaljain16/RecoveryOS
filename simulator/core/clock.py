"""
Discrete virtual timeline clock for RecoveryOS Simulator.
Allows reproducible simulation across time windows, 15-minute anomaly buckets,
and temporal degradation waves.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class SimClock:
    """
    Virtual simulation clock managing current time progression.
    """

    def __init__(self, start_time: datetime | None = None):
        # Default start: fixed baseline date to ensure deterministic timestamps
        if start_time is None:
            self.current_time = datetime(2026, 8, 20, 9, 0, 0, tzinfo=timezone.utc)
        else:
            if start_time.tzinfo is None:
                self.current_time = start_time.replace(tzinfo=timezone.utc)
            else:
                self.current_time = start_time

    def tick(self, seconds: float = 1.0) -> datetime:
        """Advance time by a specified number of seconds and return the new time."""
        self.current_time += timedelta(seconds=seconds)
        return self.current_time

    def advance(self, td: timedelta) -> datetime:
        """Advance time by a timedelta and return the new time."""
        self.current_time += td
        return self.current_time

    def get_time(self) -> datetime:
        """Get the current simulation time."""
        return self.current_time

    def get_15m_bucket(self) -> datetime:
        """Get the aligned 15-minute floor bucket for anomaly tracking (TRD §3.2)."""
        dt = self.current_time
        minute = (dt.minute // 15) * 15
        return dt.replace(minute=minute, second=0, microsecond=0)
