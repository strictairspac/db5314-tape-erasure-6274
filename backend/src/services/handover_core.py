"""档案交接册的纯领域规则。

这里不依赖 FastAPI、SQLAlchemy 或第三方包，便于直接用标准库 unittest 验证：

* 受理时只确定 ``watermark``（当时最大执行批次号），之后新批次号更大，不进册；
* 按批次 id 键集分页，检查点只有一个单调游标，内存不随历史批次总数增长；
* 收录幂等键是 ``(book_id, run_id)``，重试只更新同一行，不产生重复批次；
* 执行完工、擦除证据且摘要一致、复核已签署三者齐备，才算该批次对齐。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

# 册状态。ready/complete 都视为可下载，兼容不同测试和接口命名。
BUILDING = 'building'
PENDING = 'pending'
WAITING = 'waiting'
READY = 'ready'
COMPLETE = 'complete'
FAILED = 'failed'

OPEN_STATUSES = {'queued', 'running', 'pending', 'processing', 'claimed'}
DONE_STATUSES = {'complete', 'completed', 'done', 'succeeded', 'success'}
FAIL_STATUSES = {'failed', 'error', 'errored', 'failure'}

BOOK_STATUSES = {BUILDING, PENDING, WAITING, READY, COMPLETE, FAILED}
DOWNLOADABLE_STATUSES = {READY, COMPLETE}


class HandoverError(RuntimeError):
    """交接册制作规则错误。"""


class HandoverNotReady(HandoverError):
    """尚有批次未对齐，不能下载。"""

    def __init__(self, message: str = '交接册尚未全部对齐，暂不开放下载'):
        super().__init__(message)
        self.status_code = 409


class BatchSource(Protocol):
    def max_batch_id(self) -> int: ...

    def batch_page(self, after_id: int, limit: int, watermark: int) -> Sequence[Mapping[str, Any]]: ...


class CheckpointStore(Protocol):
    def load_cursor(self, book_id: Any) -> int: ...

    def save_cursor(self, book_id: Any, cursor: int, scan_complete: bool) -> None: ...

    def upsert_item(self, book_id: Any, item: Mapping[str, Any]) -> None: ...


def as_mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    if hasattr(value, '_asdict'):
        return value._asdict()
    return dict(value)


def first_key(row: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


def batch_identity(batch: Mapping[str, Any]) -> int | None:
    value = first_key(batch, 'run_id', 'batch_id', 'id', 'execution_id')
    return None if value is None else int(value)


def snapshot_watermark(batches: Iterable[Mapping[str, Any]] | None = None,
                       *, max_id: int | None = None) -> int:
    """受理时高水位：显式 max_id 优先，否则取给定批次中的最大 id。"""
    if max_id is not None:
        return int(max_id)
    highest = 0
    for batch in batches or ():
        batch = as_mapping(batch)
        identity = batch_identity(batch)
        if identity is not None:
            highest = max(highest, identity)
    return highest


def keyset_page(batches: Iterable[Mapping[str, Any]], after_id: int = 0,
                limit: int = 100, watermark: int | None = None) -> list[Mapping[str, Any]]:
    """从已按 id 排序的有限迭代源中取一页。

    调用方通常应让数据源在 SQL 中完成 ``id > after_id AND id <= watermark``，
    此函数主要供纯领域测试和内存假源使用。
    """
    if limit <= 0:
        raise ValueError('分页大小必须为正整数')
    page: list[Mapping[str, Any]] = []
    for raw in batches:
        row = as_mapping(raw)
        identity = batch_identity(row)
        if identity is None or identity <= int(after_id):
            continue
        if watermark is not None and identity > int(watermark):
            break
        page.append(row)
        if len(page) >= limit:
            break
    return page


def _signed(review: Mapping[str, Any]) -> bool:
    if not review:
        return False
    signed = first_key(review, 'signed', 'is_signed', default=None)
    if isinstance(signed, bool):
        return signed
    status = str(first_key(review, 'review_status', 'status', 'sign_status', default='')).lower()
    if status in {'signed', 'reviewed', 'complete', 'completed', 'approved'}:
        return True
    return bool(first_key(review, 'reviewer', 'signed_by', 'actor')
                and first_key(review, 'reviewed_at', 'signed_at', 'timestamp', 'at'))


def align_batch(batch: Mapping[str, Any] | None = None,
                evidence: Mapping[str, Any] | None = None,
                review: Mapping[str, Any] | None = None,
                **extra: Any) -> dict[str, Any]:
    """把执行批次、擦除证据和签署状态对齐成一条交接册条目。

    输入尽量宽容：调用方可以分别传 ``batch/evidence/review``，也可以把对应行
    合并在一个 mapping 中。返回值始终可 JSON 序列化。
    """
    row = dict(as_mapping(batch))
    ev = dict(as_mapping(evidence))
    rv = dict(as_mapping(review))
    for source in (extra, ev, rv):
        for key, value in source.items():
            row.setdefault(key, value)

    run_id = batch_identity(row)
    execution_status = str(first_key(
        row, 'execution_status', 'run_status', 'status', default='') or '').lower()
    run_error = first_key(row, 'error', 'run_error', 'error_message')

    evidence_id = first_key(row, 'evidence_id', 'conclusion_id')
    if evidence_id is None:
        evidence_id = first_key(ev, 'id')
    digest = first_key(row, 'evidence_digest', 'digest') or first_key(ev, 'digest')
    digest_valid = first_key(row, 'digest_valid', 'checksum_valid', default=None)
    if digest_valid is None:
        digest_valid = first_key(ev, 'digest_valid', 'checksum_valid', default=None)
    if digest_valid is None:
        # 没有显式校验结果时，只要有证据和摘要，服务层可另行重算；纯规则不猜 false。
        digest_valid = bool(evidence_id and digest)

    reviewer = first_key(row, 'reviewer', 'signed_by') or first_key(rv, 'reviewer', 'signed_by')
    review_note = first_key(row, 'review_note', 'note') or first_key(rv, 'review_note', 'note')
    reviewed_at = first_key(row, 'reviewed_at', 'signed_at') or first_key(rv, 'reviewed_at', 'signed_at')
    report = as_mapping(row.get('report') or ev.get('report'))
    review_status = str(first_key(
        row, 'review_status', 'sign_status', default='') or '').lower()
    if not review_status:
        review_status = 'signed' if (reviewer and reviewed_at) or _signed(rv) else 'unsigned'

    issues: list[str] = []
    execution_aligned = execution_status in DONE_STATUSES
    if not execution_aligned:
        if execution_status in FAIL_STATUSES:
            issues.append(f'执行批次失败：{run_error or execution_status or "未知错误"}')
        elif execution_status in OPEN_STATUSES:
            issues.append(f'执行批次尚未完工：{execution_status}')
        else:
            issues.append(f'执行批次状态不可对齐：{execution_status or "missing"}')

    erasure_aligned = bool(evidence_id) and bool(digest_valid)
    if not evidence_id:
        issues.append('缺少擦除结论证据')
    elif digest_valid is False:
        issues.append('擦除结论校验码不一致')

    signature_aligned = review_status in {'signed', 'reviewed', 'approved'} or bool(reviewer and reviewed_at)
    if not signature_aligned:
        issues.append('擦除结论尚未签署')

    aligned = execution_aligned and erasure_aligned and signature_aligned
    result = {
        'run_id': run_id,
        'case_id': first_key(row, 'case_id'),
        'execution_status': execution_status or None,
        'evidence_id': evidence_id,
        'erasure_status': ('verified' if erasure_aligned else
                           'missing' if not evidence_id else 'digest_mismatch'),
        'conclusion': first_key(row, 'conclusion', 'summary') or first_key(ev, 'summary')
                      or first_key(report, 'summary'),
        'metrics': first_key(row, 'metrics') or first_key(ev, 'metrics')
                   or first_key(report, 'metrics') or {},
        'evidence_digest': digest,
        'digest_valid': bool(digest_valid),
        'review_status': review_status,
        'reviewer': reviewer,
        'review_note': review_note,
        'reviewed_at': reviewed_at,
        'aligned': aligned,
        'issues': issues,
    }
    return result


@dataclass
class HandoverCheckpoint:
    """制作游标。只保存最后处理到的批次号，不保存已收录 id 集合。"""

    watermark: int = 0
    cursor: int = 0
    scan_complete: bool = False
    processed: int = 0
    aligned: int = 0

    def advance(self, page: Sequence[Mapping[str, Any]]) -> int:
        if not page:
            self.scan_complete = True
            return self.cursor
        last = self.cursor
        for raw in page:
            identity = batch_identity(as_mapping(raw))
            if identity is not None:
                last = max(last, identity)
        if len(page) == 0:
            self.scan_complete = True
        self.cursor = last
        self.processed += len(page)
        return self.cursor


@dataclass
class HandoverBookBuilder:
    """通用分页制作器。

    ``fetch_page(after_id, limit, watermark)`` 必须只返回下一页；``persist`` 负责
    幂等落库。``on_page`` 可用于测试注入中断。制作器自身不累积条目。
    """

    watermark: int = 0
    page_size: int = 100
    fetch_page: Callable[[int, int, int], Sequence[Mapping[str, Any]]] | None = None
    persist: Callable[[Sequence[dict[str, Any]], HandoverCheckpoint], None] | None = None
    align: Callable[..., dict[str, Any]] = align_batch
    checkpoint: HandoverCheckpoint = field(default_factory=HandoverCheckpoint)
    max_pages: int | None = None

    def __post_init__(self) -> None:
        if self.page_size <= 0:
            raise ValueError('page_size 必须为正整数')
        self.checkpoint.watermark = int(self.watermark)

    def run_once(self) -> list[dict[str, Any]]:
        if self.fetch_page is None:
            raise HandoverError('缺少 fetch_page 数据源')
        page = self.fetch_page(self.checkpoint.cursor, self.page_size, self.checkpoint.watermark)
        aligned_items = [self.align(item) for item in page]
        if self.persist is not None:
            self.persist(aligned_items, self.checkpoint)
        self.checkpoint.advance(page)
        if len(page) < self.page_size:
            self.checkpoint.scan_complete = True
        return aligned_items

    def run(self, *, interrupt_after_pages: int | None = None) -> HandoverCheckpoint:
        pages = 0
        while not self.checkpoint.scan_complete:
            self.run_once()
            pages += 1
            if interrupt_after_pages is not None and pages >= interrupt_after_pages:
                raise HandoverError('模拟制作中断')
            if self.max_pages is not None and pages >= self.max_pages:
                break
        return self.checkpoint


def all_aligned(items: Iterable[Mapping[str, Any]]) -> bool:
    for item in items:
        if not as_mapping(item).get('aligned'):
            return False
    # 空交接册 vacuously ready，与“没有批次也可正常出册”的边界一致。
    return True


def require_downloadable(status: str) -> None:
    if str(status).lower() not in DOWNLOADABLE_STATUSES:
        raise HandoverNotReady()
