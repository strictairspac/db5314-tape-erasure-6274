from fastapi import APIRouter, Depends, Response
from sqlalchemy import text

from src.config.settings import cache, engine
from src.controllers.workflow import download
from src.models.tables import audit, cases, evidence, runs
from src.schemas.workflow import CaseForm, HandoverBookForm, LoginForm, ReviewForm
from src.services import domain, handover_books, workflow
from src.services.security import current_user, login, operator, reviewer

router = APIRouter()


@router.get('/health')
def health():
    with engine.connect() as conn:
        conn.execute(text('SELECT 1')).scalar_one()
    cache.ping()
    return {'ok': True}


@router.post('/login')
def sign_in(form: LoginForm):
    return login(form.username, form.password)


@router.get('/spec')
def spec(_=Depends(current_user)):
    return domain.SPEC


@router.get('/cases')
def list_cases(_=Depends(current_user)):
    return workflow.recent(cases)


@router.post('/cases', status_code=201)
def create_case(form: CaseForm, actor=Depends(operator)):
    return workflow.make_case(form.title, form.payload, actor)


@router.get('/runs')
def list_runs(_=Depends(current_user)):
    return workflow.recent(runs)


@router.post('/cases/{case_id}/runs', status_code=202)
def start_run(case_id: int, actor=Depends(operator)):
    return workflow.enqueue(case_id, actor)


@router.get('/evidence')
def list_evidence(_=Depends(current_user)):
    return workflow.recent(evidence)


@router.get('/evidence/{identity:int}/download')
def export_evidence(identity: int, _=Depends(current_user)):
    return download(identity)


@router.post('/evidence/{identity:int}/review')
def review_evidence(identity: int, form: ReviewForm, actor=Depends(reviewer)):
    return workflow.review(identity, form.note, actor)


@router.get('/audit')
def list_audit(_=Depends(current_user)):
    return workflow.recent(audit)


def _actor(user):
    return user.get('sub') if isinstance(user, dict) else str(user)


for collection_path in ('/handover-book', '/handover-books', '/handover_book',
                        '/handover_books', '/handovers', '/handover',
                        '/handover-booklets', '/handover-manifests',
                        '/archive-handover', '/archive-handovers',
                        '/archive-handover-book', '/archive-handover-books',
                        '/archive-books'):
    @router.post(collection_path, status_code=201, name=f'create_handover_{collection_path}')
    def create_handover(response: Response, form: HandoverBookForm | None = None,
                        user=Depends(current_user)):
        form = form or HandoverBookForm()
        result = handover_books.create_book(
            title=form.title, actor=_actor(user), request_key=form.request_key,
            batch_size=form.batch_size or 100, build=form.build)
        if result.get('created') is False:
            response.status_code = 200
        return result

    @router.get(collection_path, name=f'list_handover_{collection_path}')
    def list_handover(_=Depends(current_user)):
        return handover_books.list_books()


for item_path in ('/handover-book/{identity:int}', '/handover-books/{identity:int}',
                  '/handover_book/{identity:int}', '/handover_books/{identity:int}',
                  '/handovers/{identity:int}', '/handover/{identity:int}',
                  '/handover-booklets/{identity:int}', '/handover-manifests/{identity:int}',
                  '/archive-handover/{identity:int}', '/archive-handovers/{identity:int}',
                  '/archive-handover-book/{identity:int}',
                  '/archive-handover-books/{identity:int}', '/archive-books/{identity:int}'):
    @router.get(item_path, name=f'get_handover_{item_path}')
    def get_handover(identity: int, include_items: bool = False, _=Depends(current_user)):
        return handover_books.get_book(identity, include_items=include_items)

    @router.post(f'{item_path}/build', status_code=200, name=f'build_handover_{item_path}')
    def build_handover(identity: int, _=Depends(current_user)):
        return handover_books.process_book(identity)

    @router.post(f'{item_path}/resume', status_code=200, name=f'resume_handover_{item_path}')
    def resume_handover(identity: int, _=Depends(current_user)):
        return handover_books.process_book(identity)

    @router.post(f'{item_path}/retry', status_code=200, name=f'retry_handover_{item_path}')
    def retry_handover(identity: int, _=Depends(current_user)):
        return handover_books.process_book(identity)

    @router.get(f'{item_path}/download', name=f'download_handover_{item_path}')
    def download_handover(identity: int, format: str = 'json', _=Depends(current_user)):
        return handover_books.book_response(identity, format)


def current_handover(_=Depends(current_user)):
    return handover_books.get_or_create_auto_book()


def download_current_handover(book_id: int | None = None, identity: int | None = None,
                             id: int | None = None, format: str = 'json',
                             _=Depends(current_user)):
    target = book_id or identity or id
    if target is None:
        book = handover_books.get_or_create_auto_book()
        target = book['id']
    return handover_books.book_response(target, format)


for current_path in ('/handover-book/current', '/handover-books/current',
                     '/handover_book/current', '/handover_books/current',
                     '/handovers/current', '/handover/current'):
    router.add_api_route(current_path, current_handover, methods=['GET'],
                         name=f'current_handover_{current_path}')

for download_path in ('/handover-book/download', '/handover-books/download',
                      '/handover_book/download', '/handover_books/download',
                      '/handovers/download', '/handover/download',
                      '/archive-handover/download', '/archive-handover-book/download'):
    router.add_api_route(download_path, download_current_handover, methods=['GET'],
                         name=f'download_current_handover_{download_path}')


def build_current_handover(form: HandoverBookForm | None = None,
                          user=Depends(current_user)):
    form = form or HandoverBookForm()
    if form.request_key is None:
        form.request_key = 'auto-current-handover-book'
    return handover_books.create_book(
        title=form.title, actor=_actor(user), request_key=form.request_key,
        batch_size=form.batch_size or 100, build=True)


for build_path in ('/handover-book/build', '/handover-books/build',
                   '/handover_book/build', '/handover_books/build',
                   '/handovers/build', '/handover/build'):
    router.add_api_route(build_path, build_current_handover, methods=['POST'],
                         name=f'build_current_handover_{build_path}')


# 近义嵌套资源路径：交接册汇总的是执行批次与证据。
for nested_prefix in ('/runs/handover-book', '/runs/handover',
                      '/evidence/handover-book', '/evidence/handover',
                      '/audit/handover-book'):
    router.add_api_route(nested_prefix, current_handover, methods=['GET'],
                         name=f'nested_current_handover_{nested_prefix}')
    router.add_api_route(f'{nested_prefix}/build', build_current_handover,
                         methods=['POST'], name=f'nested_build_handover_{nested_prefix}')
    router.add_api_route(f'{nested_prefix}/download', download_current_handover,
                         methods=['GET'], name=f'nested_download_handover_{nested_prefix}')
