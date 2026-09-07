from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime:
        ...


class RealClock(Clock):
    def now(self) -> datetime:
        return datetime.now().astimezone()


class FixedClock(Clock):
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value

    def set(self, value: datetime) -> None:
        self.value = value
