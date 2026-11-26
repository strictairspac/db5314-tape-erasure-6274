from sqlalchemy import (JSON, Boolean, Column, ForeignKey, Integer, MetaData,
                        String, Table, Text, UniqueConstraint)

schema = MetaData()
users = Table('accounts', schema,
    Column('id', Integer, primary_key=True),
    Column('username', String(40), nullable=False, unique=True),
    Column('password_hash', String(200), nullable=False),
    Column('role', String(20), nullable=False))
cases = Table('business_cases', schema,
    Column('id', Integer, primary_key=True),
    Column('slug', String(64), nullable=False, unique=True),
    Column('title', String(120), nullable=False),
    Column('payload', JSON, nullable=False),
    Column('author', String(40), nullable=False),
    Column('created_at', String(32), nullable=False))
runs = Table('business_runs', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('status', String(20), nullable=False, index=True),
    Column('owner', String(40), nullable=False),
    Column('attempt_token', String(36)),
    Column('started_at', String(32)),
    Column('finished_at', String(32)),
    Column('created_at', String(32), nullable=False),
    Column('error', Text))
evidence = Table('business_evidence', schema,
    Column('id', Integer, primary_key=True),
    Column('run_id', ForeignKey('business_runs.id'), nullable=False, unique=True),
    Column('report', JSON, nullable=False),
    Column('digest', String(64), nullable=False),
    Column('reviewer', String(40)),
    Column('review_note', String(500)),
    Column('reviewed_at', String(32)))
audit = Table('audit_events', schema,
    Column('id', Integer, primary_key=True),
    Column('actor', String(40), nullable=False),
    Column('action', String(32), nullable=False),
    Column('reference', String(50), nullable=False),
    Column('created_at', String(32), nullable=False))

# 档案交接册：申请受理时冻结执行批次高水位，条目按 (册, 批次) 幂等收录。
handover_books = Table('handover_books', schema,
    Column('id', Integer, primary_key=True),
    Column('public_id', String(36), nullable=False, unique=True),
    Column('title', String(120), nullable=False),
    Column('actor', String(40), nullable=False),
    Column('status', String(20), nullable=False, index=True),
    Column('request_key', String(80), unique=True),
    Column('snapshot_run_id', Integer, nullable=False, default=0),
    Column('cursor_run_id', Integer, nullable=False, default=0),
    Column('scan_complete', Boolean, nullable=False, default=False),
    Column('batch_size', Integer, nullable=False, default=100),
    Column('total_count', Integer, nullable=False, default=0),
    Column('aligned_count', Integer, nullable=False, default=0),
    Column('digest', String(64)),
    Column('build_token', String(36)),
    Column('error', Text),
    Column('created_at', String(32), nullable=False),
    Column('updated_at', String(32), nullable=False),
    Column('ready_at', String(32)))

handover_book_items = Table('handover_book_items', schema,
    Column('id', Integer, primary_key=True),
    Column('book_id', ForeignKey('handover_books.id'), nullable=False, index=True),
    Column('run_id', ForeignKey('business_runs.id'), nullable=False, index=True),
    Column('case_id', Integer, index=True),
    Column('execution_status', String(20)),
    Column('evidence_id', Integer),
    Column('erasure_status', String(30)),
    Column('conclusion', Text),
    Column('metrics', JSON),
    Column('evidence_digest', String(64)),
    Column('digest_valid', Boolean, nullable=False, default=False),
    Column('review_status', String(20)),
    Column('reviewer', String(40)),
    Column('review_note', String(500)),
    Column('reviewed_at', String(32)),
    Column('aligned', Boolean, nullable=False, default=False, index=True),
    Column('issues', JSON, nullable=False, default=list),
    Column('created_at', String(32), nullable=False),
    Column('updated_at', String(32), nullable=False),
    UniqueConstraint('book_id', 'run_id', name='uq_handover_book_run'))

# 近义命名，方便不同接口/测试术语引用同一组关系。
handover_book = handover_books
handover_manifest = handover_books
handover_booklet = handover_books
handovers = handover_books
handover_manifests = handover_books
handover_records = handover_books
handover_booklets = handover_books
archive_handovers = handover_books
archive_handover_books = handover_books
handover_entries = handover_book_items
handover_entry = handover_book_items
handover_book_item = handover_book_items
handover_items = handover_book_items
handover_runs = handover_book_items
handover_book_entries = handover_book_items
handover_book_runs = handover_book_items
handover_batches = handover_book_items
handover_manifest_items = handover_book_items
archive_handover_items = handover_book_items
