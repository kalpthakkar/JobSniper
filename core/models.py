"""
core/models.py — Shared data models for Job Sniper
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Priority(Enum):
    HIGH = "HIGH"
    MID  = "MID"
    LOW  = "LOW"


class ATSType(Enum):
    GREENHOUSE = "greenhouse"
    ASHBY      = "ashby"
    WORKABLE   = "workable"
    WORKDAY    = "workday"
    LEVER      = "lever"

@dataclass
class Company:
    """A company to monitor, loaded from config."""
    name: str
    board_token: str
    ats: ATSType
    priority: Priority
    enabled: bool = True

    def __hash__(self):
        return hash(self.board_token + self.ats.value)


@dataclass
class Job:
    """Normalised job posting — ATS-agnostic."""
    id: str
    title: str
    company: str
    location: str
    department: str
    url: str
    posted_at: Optional[str] = None
    remote: bool = False
    salary: Optional[str] = None
    raw: dict = field(default_factory=dict)  # original payload

    def short_repr(self) -> str:
        loc = f" [{self.location}]" if self.location else ""
        dept = f" | {self.department}" if self.department else ""
        salary = f" 💰 {self.salary}" if self.salary else ""
        return f"{self.title}{loc}{dept}{salary}\n   🔗 {self.url}"
