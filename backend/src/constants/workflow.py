from enum import StrEnum


class Role(StrEnum):
    OPERATOR = 'operator'
    REVIEWER = 'reviewer'
    VIEWER = 'viewer'


class Status(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    COMPLETE = 'complete'
    FAILED = 'failed'


class HandoverStatus(StrEnum):
    BUILDING = 'building'
    PENDING = 'pending'
    WAITING = 'waiting'
    READY = 'ready'
    COMPLETE = 'complete'
    FAILED = 'failed'


# 近义命名，兼容不同测试/前端枚举习惯。
HandoverBookStatus = HandoverStatus
