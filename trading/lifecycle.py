"""Idempotent lifecycle state for the live trader."""

from __future__ import annotations

from enum import Enum


class LifecycleState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


class LifecycleController:
    def __init__(self) -> None:
        self.state = LifecycleState.STOPPED

    def begin_start(self) -> bool:
        if self.state in (LifecycleState.STARTING, LifecycleState.RUNNING):
            return False
        self.state = LifecycleState.STARTING
        return True

    def mark_running(self) -> None:
        self.state = LifecycleState.RUNNING

    def fail_start(self) -> None:
        self.state = LifecycleState.STOPPED

    def begin_stop(self) -> bool:
        if self.state in (LifecycleState.STOPPED, LifecycleState.STOPPING):
            return False
        self.state = LifecycleState.STOPPING
        return True

    def mark_stopped(self) -> None:
        self.state = LifecycleState.STOPPED
