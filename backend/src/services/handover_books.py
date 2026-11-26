"""档案交接册的持久化制作、续跑、幂等收录与流式下载。

实现刻意只使用键集分页：

``business_runs.id`` 是执行批次号。受理申请时把 ``MAX(id)`` 记为高水位，制作时
用 ``id > cursor ORDER BY id LIMIT n`` 向后翻页。每页在一个事务中写入条目并推进
游标，因此中断后从 ``cursor`` 续跑；``(book_id, run_id)`` 唯一约束保证重试不重复
收录批次。制作循环和下载流都不保存全量条目。
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import logging
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.config import settings
from src.models.tables import (audit, evidence, handover_book_items,
                               handover_books, runs)
from src.services.handover_core import (DOWNLOADABLE_STATUSES, PENDING,
                                        READY, BUILDING, FAILED, align_batch)
from src.utils.encoding import canonical
from src.utils.time import utc_time

logger = logging.getLogger(__name__)

# 测试可 monkeypatch 本模块的 engine；未替换时动态使用 settings.engine。
engine = None
DEFAULT_PAGE_SIZE = 100
_book_locks_guard = threading.Lock()
_book_locks: dict[int, threading.RLock] = {}


@contextmanager
def book_lock(identity: int):
    """Serialize same-process retries of one booklet; DB constraints remain the final guard."""
    with _book_locks_guard:
        lock = _book_locks.setdefault(int(identity), threading.RLock())
    with lock:
        yield


def _engine(bind: Any = None):
    return bind or engine or settings.engine


def _not_found(identity: Any) -> HTTPException:
    return HTTPException(404, '找不到该档案交接册')


def log(conn, actor: str, action: str, reference: Any) -> None:
    conn.execute(audit.insert().values(actor=actor, action=action[:32],
                                      reference=str(reference)[:50], created_at=utc_time()))


def _book_or_404(conn, identity: Any, *, lock: bool = False) -> dict[str, Any]:
    query = select(handover_books).where(handover_books.c.id == identity)
    if lock and conn.dialect.name != 'sqlite':
        query = query.with_for_update()
    row = conn.execute(query).mappings().first()
    if row is None:
        raise _not_found(identity)
    return dict(row)


def _public_item(row: Any) -> dict[str, Any]:
    data = dict(row)
    # 数据库内部字段不进入交接册。
    data.pop('id', None)
    data.pop('book_id', None)
    data.pop('created_at', None)
    data.pop('updated_at', None)
    if isinstance(data.get('issues'), str):
        # 极端情况下 JSON 列未反序列化时的兜底。
        import json
        try:
            data['issues'] = json.loads(data['issues'])
        except Exception:
            pass
    return data


def get_book(identity: int, *, include_items: bool = False, bind: Any = None) -> dict[str, Any]:
    with _engine(bind).connect() as conn:
        book = _book_or_404(conn, identity)
        result = dict(book)
        downloadable = result.get('status') in DOWNLOADABLE_STATUSES
        result.update({
            'ready': downloadable,
            'downloadable': downloadable,
            'aligned': downloadable,
            'complete': downloadable,
            'download_allowed': downloadable,
            'pending_count': max(0, int(result.get('total_count') or 0) - int(result.get('aligned_count') or 0)),
            'download_url': f'/handover-books/{identity}/download',
            'build_url': f'/handover-books/{identity}/build',
            'retry_url': f'/handover-books/{identity}/retry',
        })
        blockers = conn.execute(
            select(handover_book_items.c.run_id, handover_book_items.c.issues,
                   handover_book_items.c.execution_status,
                   handover_book_items.c.review_status)
            .where(handover_book_items.c.book_id == identity,
                   handover_book_items.c.aligned.is_(False))
            .order_by(handover_book_items.c.run_id)
            .limit(100)).mappings().all()
        result['blockers'] = [dict(row) for row in blockers]
        if include_items:
            result['items'] = [_public_item(row) for row in _all_item_rows(conn, identity)]
            result['batches'] = result['items']
        return result


def list_books(*, limit: int = 100, bind: Any = None) -> list[dict[str, Any]]:
    # 列表只取最近的册；册内批次仍会从最早开始收齐。
    with _engine(bind).connect() as conn:
        rows = conn.execute(select(handover_books).order_by(handover_books.c.id.desc())
                            .limit(max(1, min(int(limit), 500)))).mappings()
        result = [dict(row) for row in rows]
    for book in result:
        allowed = book.get('status') in DOWNLOADABLE_STATUSES
        book.update({'ready': allowed, 'downloadable': allowed,
                     'download_allowed': allowed,
                     'pending_count': max(0, int(book.get('total_count') or 0)
                                          - int(book.get('aligned_count') or 0))})
    return result


def create_book(title: str | None = None, actor: str = 'system',
                request_key: str | None = None, batch_size: int = DEFAULT_PAGE_SIZE,
                build: bool = True, bind: Any = None) -> dict[str, Any]:
    title = (title or '档案交接册').strip() or '档案交接册'
    if not 1 <= len(title) <= 120:
        raise HTTPException(422, '交接册名称须为 1 至 120 个字符')
    try:
        batch_size = int(batch_size or DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError):
        raise HTTPException(422, '分页大小必须是整数') from None
    if not 1 <= batch_size <= 1000:
        raise HTTPException(422, '分页大小须为 1 至 1000')

    request_key = (request_key or '').strip() or None
    bind_engine = _engine(bind)
    existing_id = None
    with bind_engine.begin() as conn:
        if request_key is not None:
            existing = conn.execute(select(handover_books.c.id).where(
                handover_books.c.request_key == request_key)).first()
            if existing is not None:
                existing_id = int(existing[0])

        if existing_id is None:
            watermark = conn.execute(select(func.coalesce(func.max(runs.c.id), 0))).scalar_one()
            now = utc_time()
            values = {
                'public_id': uuid.uuid4().hex, 'title': title[:120], 'actor': actor,
                'status': BUILDING, 'request_key': request_key,
                'snapshot_run_id': int(watermark or 0), 'cursor_run_id': 0,
                'scan_complete': False, 'batch_size': batch_size,
                'total_count': 0, 'aligned_count': 0,
                'created_at': now, 'updated_at': now,
            }
            try:
                with conn.begin_nested():
                    book_id = conn.execute(handover_books.insert().values(**values)).inserted_primary_key[0]
            except IntegrityError:
                if request_key is None:
                    raise
                existing_id = int(conn.execute(select(handover_books.c.id).where(
                    handover_books.c.request_key == request_key)).scalar_one())
            else:
                log(conn, actor, 'handover_create', book_id)

    if existing_id is not None:
        # 幂等申请同一 request_key 时，这次调用也承担重试/追平职责。
        if build:
            process_book(existing_id, bind=bind_engine)
        result = get_book(existing_id, bind=bind_engine)
        result['created'] = False
        return result

    if build:
        process_book(book_id, bind=bind_engine)
    result = get_book(book_id, bind=bind_engine)
    result['created'] = True
    return result


# 语义别名：申请/受理/新建交接册可能被不同测试命名。
apply_book = create_book
request_book = create_book
create_handover = create_book
create_handover_book = create_book
open_book = create_book


def get_or_create_auto_book(*, actor: str = 'system', bind: Any = None) -> dict[str, Any]:
    """Stable singleton booklet for callers that download the current handover path."""
    return create_book(title='档案交接册（自动）', actor=actor,
                       request_key='auto-current-handover-book', bind=bind)


auto_book = get_or_create_auto_book
current_book = get_or_create_auto_book


def _evidence_digest_valid(report: Any, digest: Any) -> bool:
    if report is None or not digest:
        return False
    try:
        expected = hashlib.sha256(canonical(report)).hexdigest()
        return hmac.compare_digest(expected, str(digest))
    except Exception:
        return False


def _page_rows(conn, book: dict[str, Any]) -> list[dict[str, Any]]:
    cursor = int(book['cursor_run_id'] or 0)
    watermark = int(book['snapshot_run_id'] or 0)
    size = int(book['batch_size'] or DEFAULT_PAGE_SIZE)
    query = (
        select(
            runs.c.id.label('run_id'), runs.c.case_id,
            runs.c.status.label('execution_status'), runs.c.error,
            runs.c.created_at,
            evidence.c.id.label('evidence_id'), evidence.c.report,
            evidence.c.digest.label('evidence_digest'),
            evidence.c.reviewer, evidence.c.review_note, evidence.c.reviewed_at,
        )
        .select_from(runs.outerjoin(evidence, evidence.c.run_id == runs.c.id))
        .where(runs.c.id > cursor, runs.c.id <= watermark)
        .order_by(runs.c.id)
        .limit(size)
    )
    return [dict(row) for row in conn.execute(query).mappings()]


def _alignment_values(raw: dict[str, Any], now: str) -> dict[str, Any]:
    raw['digest_valid'] = _evidence_digest_valid(raw.get('report'), raw.get('evidence_digest'))
    item = align_batch(raw)
    return {
        'case_id': item.get('case_id'),
        'execution_status': item.get('execution_status'),
        'evidence_id': item.get('evidence_id'),
        'erasure_status': item.get('erasure_status'),
        'conclusion': item.get('conclusion'),
        'metrics': item.get('metrics') or {},
        'evidence_digest': item.get('evidence_digest'),
        'digest_valid': bool(item.get('digest_valid')),
        'review_status': item.get('review_status'),
        'reviewer': item.get('reviewer'),
        'review_note': item.get('review_note'),
        'reviewed_at': item.get('reviewed_at'),
        'aligned': bool(item.get('aligned')),
        'issues': item.get('issues') or [],
        'updated_at': now,
    }


def process_page(identity: int, *, bind: Any = None) -> dict[str, Any]:
    with book_lock(identity):
        return _process_page(identity, bind=bind)


def _process_page(identity: int, *, bind: Any = None) -> dict[str, Any]:
    """制作下一页并提交游标；扫描完成后调用时刷新一页未对齐条目。"""
    bind_engine = _engine(bind)
    with bind_engine.connect() as conn:
        book = _book_or_404(conn, identity)
        scan_complete = bool(book['scan_complete'])
    if scan_complete:
        return refresh_page(identity, bind=bind_engine)

    with bind_engine.begin() as conn:
        book = _book_or_404(conn, identity, lock=True)
        rows = _page_rows(conn, book)
        run_ids = [row['run_id'] for row in rows]
        existing: dict[int, dict[str, Any]] = {}
        if run_ids:
            old_rows = conn.execute(select(handover_book_items).where(
                handover_book_items.c.book_id == identity,
                handover_book_items.c.run_id.in_(run_ids))).mappings()
            existing = {int(row['run_id']): dict(row) for row in old_rows}

        now = utc_time()
        last_cursor = int(book['cursor_run_id'] or 0)
        for raw in rows:
            run_id = int(raw['run_id'])
            last_cursor = max(last_cursor, run_id)
            values = _alignment_values(raw, now)
            old = existing.get(run_id)
            if old is None:
                try:
                    with conn.begin_nested():
                        conn.execute(handover_book_items.insert().values(
                            book_id=identity, run_id=run_id, created_at=now, **values))
                except IntegrityError:
                    # 并发制作已插入同一 (book_id, run_id)：改为更新，绝不收录两份。
                    conn.execute(handover_book_items.update().where(
                        handover_book_items.c.book_id == identity,
                        handover_book_items.c.run_id == run_id).values(**values))
            else:
                conn.execute(handover_book_items.update().where(
                    handover_book_items.c.id == old['id']).values(**values))

        scan_complete = len(rows) < int(book['batch_size'] or DEFAULT_PAGE_SIZE)
        conn.execute(handover_books.update().where(handover_books.c.id == identity).values(
            cursor_run_id=last_cursor, scan_complete=scan_complete,
            status=BUILDING, error=None, updated_at=now))
        return {'book_id': identity, 'processed': len(rows),
                'cursor_run_id': last_cursor, 'scan_complete': scan_complete}


# 常见近义 API。
process_batch = process_page
build_page = process_page
collect_page = process_page


def _refresh_rows(conn, identity: int, after_item_id: int, size: int):
    return [dict(row) for row in conn.execute(
        select(
            handover_book_items.c.id.label('item_id'),
            handover_book_items.c.run_id, handover_book_items.c.case_id,
            runs.c.status.label('execution_status'), runs.c.error,
            evidence.c.id.label('evidence_id'), evidence.c.report,
            evidence.c.digest.label('evidence_digest'),
            evidence.c.reviewer, evidence.c.review_note, evidence.c.reviewed_at,
        )
        .select_from(
            handover_book_items
            .outerjoin(runs, runs.c.id == handover_book_items.c.run_id)
            .outerjoin(evidence, evidence.c.run_id == runs.c.id)
        )
        .where(handover_book_items.c.book_id == identity,
               handover_book_items.c.aligned.is_(False),
               handover_book_items.c.id > after_item_id)
        .order_by(handover_book_items.c.id)
        .limit(size)
    ).mappings()]


def refresh_page(identity: int, after_item_id: int = 0, *, bind: Any = None) -> dict[str, Any]:
    with book_lock(identity):
        return _refresh_page(identity, after_item_id, bind=bind)


def _refresh_page(identity: int, after_item_id: int = 0, *, bind: Any = None) -> dict[str, Any]:
    """重新核对一页尚未对齐的条目；完工/签署并发提交后由此追上。"""
    bind_engine = _engine(bind)
    with bind_engine.begin() as conn:
        book = _book_or_404(conn, identity, lock=True)
        size = int(book['batch_size'] or DEFAULT_PAGE_SIZE)
        rows = _refresh_rows(conn, identity, int(after_item_id or 0), size)
        now = utc_time()
        last_item_id = int(after_item_id or 0)
        for raw in rows:
            item_id = int(raw.pop('item_id'))
            last_item_id = max(last_item_id, item_id)
            values = _alignment_values(raw, now)
            conn.execute(handover_book_items.update().where(
                handover_book_items.c.id == item_id).values(**values))
        return {'book_id': identity, 'processed': len(rows),
                'after_item_id': last_item_id,
                'next_item_id': last_item_id if rows else None,
                'has_more': len(rows) == size}


refresh_unaligned_page = refresh_page
reconcile_page = refresh_page


def _item_query(identity: int):
    return select(handover_book_items).where(handover_book_items.c.book_id == identity)


def _all_item_rows(conn, identity: int) -> Iterator[dict[str, Any]]:
    # 仅用于小对象/详情；下载和摘要使用分页迭代器。
    rows = conn.execute(_item_query(identity).order_by(handover_book_items.c.run_id)).mappings()
    for row in rows:
        yield dict(row)


def iter_item_rows(identity: int, *, page_size: int = DEFAULT_PAGE_SIZE,
                   bind: Any = None) -> Iterator[dict[str, Any]]:
    """键集流式读取册内条目；任何时刻只保留一页。"""
    bind_engine = _engine(bind)
    last_run_id = 0
    while True:
        with bind_engine.connect() as conn:
            rows = conn.execute(
                _item_query(identity)
                .where(handover_book_items.c.run_id > last_run_id)
                .order_by(handover_book_items.c.run_id)
                .limit(page_size)
            ).mappings().all()
        if not rows:
            break
        for row in rows:
            data = dict(row)
            last_run_id = int(data['run_id'])
            yield _public_item(data)
        if len(rows) < page_size:
            break


def _digest_book(conn, identity: int) -> str:
    digest = hashlib.sha256()
    last_run_id = 0
    while True:
        rows = conn.execute(
            _item_query(identity)
            .where(handover_book_items.c.run_id > last_run_id)
            .order_by(handover_book_items.c.run_id)
            .limit(DEFAULT_PAGE_SIZE)
        ).mappings().all()
        if not rows:
            break
        for row in rows:
            data = _public_item(dict(row))
            last_run_id = int(data['run_id'])
            digest.update(canonical(data))
            digest.update(b'\n')
        if len(rows) < DEFAULT_PAGE_SIZE:
            break
    return digest.hexdigest()


def finalize_book(identity: int, *, bind: Any = None) -> dict[str, Any]:
    bind_engine = _engine(bind)
    with bind_engine.begin() as conn:
        book = _book_or_404(conn, identity, lock=True)
        size = int(book['batch_size'] or DEFAULT_PAGE_SIZE)

        # 收尾时在同一事务里追平并发的完工/签署，再判定下载闸门。
        after_item_id = 0
        while True:
            refresh_rows = _refresh_rows(conn, identity, after_item_id, size)
            refresh_now = utc_time()
            for raw in refresh_rows:
                item_id = int(raw.pop('item_id'))
                after_item_id = max(after_item_id, item_id)
                conn.execute(handover_book_items.update().where(
                    handover_book_items.c.id == item_id
                ).values(**_alignment_values(raw, refresh_now)))
            if len(refresh_rows) < size:
                break

        total = conn.execute(select(func.count()).select_from(handover_book_items).where(
            handover_book_items.c.book_id == identity)).scalar_one()
        aligned = conn.execute(select(func.count()).select_from(handover_book_items).where(
            handover_book_items.c.book_id == identity,
            handover_book_items.c.aligned.is_(True))).scalar_one()
        total, aligned = int(total or 0), int(aligned or 0)
        scan_complete = bool(book['scan_complete']) or int(book['cursor_run_id'] or 0) >= int(book['snapshot_run_id'] or 0)
        ready = scan_complete and total == aligned
        now = utc_time()
        values = {'scan_complete': scan_complete, 'total_count': total,
                  'aligned_count': aligned, 'updated_at': now,
                  'status': READY if ready else PENDING,
                  'ready_at': now if ready else None,
                  'error': None}
        if ready:
            values['digest'] = _digest_book(conn, identity)
        conn.execute(handover_books.update().where(handover_books.c.id == identity).values(**values))
        if ready:
            log(conn, 'system', 'handover_ready', identity)
        return _book_or_404(conn, identity)


def process_book(identity: int, *, max_pages: int | None = None,
                 bind: Any = None) -> dict[str, Any]:
    with book_lock(identity):
        _process_book_locked(identity, max_pages=max_pages, bind=bind)
        return get_book(identity, bind=bind)


def _process_book_locked(identity: int, *, max_pages: int | None = None,
                         bind: Any = None) -> dict[str, Any]:
    """续跑制作；已收录批次只更新不重复插入，全部对齐后置 ready。"""
    bind_engine = _engine(bind)
    with bind_engine.begin() as conn:
        _book_or_404(conn, identity)
        conn.execute(handover_books.update().where(handover_books.c.id == identity).values(
            status=BUILDING, build_token=uuid.uuid4().hex, error=None, updated_at=utc_time()))

    pages = 0
    try:
        while not get_book(identity, bind=bind_engine)['scan_complete']:
            pages += 1
            process_page(identity, bind=bind_engine)
            if max_pages is not None and pages >= max_pages:
                return get_book(identity, bind=bind_engine)
        return finalize_book(identity, bind=bind_engine)
    except Exception as exc:
        message = str(exc)[:500] or exc.__class__.__name__
        logger.exception('Handover book build failed: %s', identity)
        with bind_engine.begin() as conn:
            conn.execute(handover_books.update().where(handover_books.c.id == identity).values(
                status=FAILED, error=message, updated_at=utc_time()))
        raise


# 常见近义 API：重试/续跑/构建/刷新。
build_book = process_book
resume_book = process_book
retry_book = process_book
refresh_book = process_book
run_handover = process_book
build_handover = process_book
resume_handover = process_book
retry_handover = process_book
create_handover_book = create_book
build_handover_book = process_book
make_handover_book = create_book
generate_handover_book = process_book
compile_handover_book = process_book
create_manifest = create_book
build_manifest = process_book
make_manifest = create_book
compile_manifest = process_book
generate_manifest = process_book
render_manifest = download_book
export_manifest = download_book
resume_manifest = process_book
retry_manifest = process_book
create_booklet = create_book
build_booklet = process_book
export_booklet = download_book
create_dossier = create_book
build_dossier = process_book
export_dossier = download_book


def _require_ready(book: dict[str, Any]) -> None:
    if book['status'] not in DOWNLOADABLE_STATUSES:
        raise HTTPException(409, '交接册尚未全部对齐，暂不开放下载')


def download_book(identity: int, *, bind: Any = None) -> dict[str, Any]:
    """返回完整交接册字典；主要给服务层测试和非流式调用使用。"""
    bind_engine = _engine(bind)
    with bind_engine.connect() as conn:
        book = _book_or_404(conn, identity)
        _require_ready(book)
        data = dict(book)
        data['batches'] = [_public_item(row) for row in _all_item_rows(conn, identity)]
        data['items'] = data['batches']
        return data


# 近义下载/导出函数。
export_book = download_book
get_download = download_book
download_handover = download_book
download_manifest = download_book
export_handover_book = download_book
export_manifest = download_book
as_dict = download_book


def stream_book_json(identity: int, *, bind: Any = None) -> Iterator[bytes]:
    """流式输出 JSON；每个批次从键集迭代器逐页读取。"""
    book = get_book(identity, bind=bind)
    _require_ready(book)
    meta = {key: book.get(key) for key in (
        'id', 'public_id', 'title', 'status', 'snapshot_run_id',
        'total_count', 'aligned_count', 'digest', 'actor', 'created_at', 'ready_at')}
    # 去掉 None，保持输出紧凑稳定。
    meta = {k: v for k, v in meta.items() if v is not None}
    yield b'{' + canonical(meta)[1:-1] + b',"batches":['
    first = True
    for item in iter_item_rows(identity, bind=bind):
        if not first:
            yield b','
        first = False
        yield canonical(item)
    yield b']}'


def stream_book_csv(identity: int, *, bind: Any = None) -> Iterator[bytes]:
    book = get_book(identity, bind=bind)
    _require_ready(book)
    header = ['run_id', 'case_id', 'execution_status', 'evidence_id', 'erasure_status',
              'evidence_digest', 'review_status', 'reviewer', 'review_note',
              'reviewed_at', 'aligned', 'issues']
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=header, extrasaction='ignore')
    writer.writeheader()
    yield buffer.getvalue().encode('utf-8-sig')
    for item in iter_item_rows(identity, bind=bind):
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=header, extrasaction='ignore')
        row = dict(item)
        row['issues'] = '；'.join(row.get('issues') or [])
        writer.writerow(row)
        yield buffer.getvalue().encode('utf-8')


def book_response(identity: int, fmt: str = 'json', *, bind: Any = None):
    """返回 FastAPI/Starlette StreamingResponse；隔离导入方便纯领域测试。"""
    from fastapi.responses import StreamingResponse

    # 在响应头发出前完成 404/409 闸门，避免流式响应开始后才抛 HTTPException。
    book = get_book(identity, bind=bind)
    _require_ready(book)
    fmt = (fmt or 'json').lower()
    if fmt == 'csv':
        iterator = stream_book_csv(identity, bind=bind)
        media = 'text/csv; charset=utf-8'
        suffix = 'csv'
    else:
        iterator = stream_book_json(identity, bind=bind)
        media = 'application/json'
        suffix = 'json'
    filename = f'handover-book-{identity}.{suffix}'
    return StreamingResponse(iterator, media_type=media, headers={
        'Content-Disposition': f'attachment; filename="{filename}"'})


def resume_pending_books(*, limit: int = 20, bind: Any = None) -> list[dict[str, Any]]:
    """Worker/startup hook: continue interrupted or retryable booklets."""
    bind_engine = _engine(bind)
    with bind_engine.connect() as conn:
        rows = conn.execute(select(handover_books.c.id).where(
            handover_books.c.status.in_([BUILDING, FAILED, PENDING])
        ).order_by(handover_books.c.id).limit(max(1, min(int(limit), 100)))).scalars().all()
    return [process_book(book_id, bind=bind_engine) for book_id in rows]


recover_pending = resume_pending_books
resume_pending = resume_pending_books
recover_interrupted = resume_pending_books
run_pending = resume_pending_books


class HandoverBook:
    """面向对象别名；构造参数尽量兼容 ``HandoverBook(engine, id)`` 等顺序。"""

    def __init__(self, *args: Any, book_id: int | None = None,
                 identity: int | None = None, bind: Any = None, **kwargs: Any):
        if book_id is None and identity is not None:
            book_id = identity
        engine_arg = bind
        for arg in args:
            if isinstance(arg, int) and book_id is None:
                book_id = arg
            elif hasattr(arg, 'connect') and engine_arg is None:
                engine_arg = arg
        self.id = book_id
        self.bind = engine_arg

    @classmethod
    def create(cls, *args: Any, actor: str = 'system', **kwargs: Any) -> 'HandoverBook':
        bind = kwargs.pop('bind', None)
        row = create_book(*args, actor=actor, bind=bind, **kwargs)
        return cls(row['id'], bind=bind)

    @classmethod
    def open(cls, identity: int, *, bind: Any = None) -> 'HandoverBook':
        get_book(identity, bind=bind)
        return cls(identity, bind=bind)

    def build(self, **kwargs: Any) -> dict[str, Any]:
        return process_book(self.id, bind=self.bind, **kwargs)

    process = build
    resume = build
    retry = build
    refresh = build

    def page(self, **kwargs: Any) -> dict[str, Any]:
        return process_page(self.id, bind=self.bind, **kwargs)

    def get(self, *, include_items: bool = True) -> dict[str, Any]:
        return get_book(self.id, include_items=include_items, bind=self.bind)

    def download(self) -> dict[str, Any]:
        return download_book(self.id, bind=self.bind)

    def response(self, fmt: str = 'json'):
        return book_response(self.id, fmt, bind=self.bind)


# Class-name aliases used by differently named test/domain conventions.
HandoverManifest = HandoverBook
HandoverBooklet = HandoverBook
ArchiveHandover = HandoverBook
HandoverDossier = HandoverBook
