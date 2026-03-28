import asyncio
import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from asgiref.sync import sync_to_async

from .models import Project, Task, ChatMessage, AgentLog


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _check_forge_secret(request):
    """Check X-Forge-Secret header for MCP channel requests."""
    return request.headers.get('X-Forge-Secret') == settings.FORGE_SECRET


def _api_auth(view_func):
    """Allow access if user is logged in OR X-Forge-Secret header is valid."""
    def wrapper(request, *args, **kwargs):
        if request.user.is_authenticated or _check_forge_secret(request):
            return view_func(request, *args, **kwargs)
        return JsonResponse({'error': 'unauthorized'}, status=401)
    wrapper.__name__ = view_func.__name__
    wrapper.__module__ = view_func.__module__
    return wrapper


# ---------------------------------------------------------------------------
# UI views
# ---------------------------------------------------------------------------

@login_required
def dashboard_view(request):
    return render(request, 'core/dashboard.html')


@login_required
def pm_chat_view(request):
    return render(request, 'core/pm_chat.html')


# ---------------------------------------------------------------------------
# Task API
# ---------------------------------------------------------------------------

@csrf_exempt
@_api_auth
@require_http_methods(['GET', 'POST'])
def api_tasks(request):
    if request.method == 'GET':
        qs = Task.objects.all()
        task_type = request.GET.get('type')
        status = request.GET.get('status')
        if task_type:
            qs = qs.filter(type=task_type)
        if status:
            qs = qs.filter(status=status)

        # Sequential enforcement: if no_active=1, only return tasks when
        # there are no active tasks anywhere
        if request.GET.get('no_active') == '1':
            if Task.objects.filter(status='active').exists():
                return JsonResponse([], safe=False)

        qs = qs.order_by('created_at')
        tasks = list(qs.values(
            'id', 'project_id', 'type', 'title', 'description',
            'priority', 'status', 'parent_id', 'created_by',
            'note', 'metadata', 'created_at', 'updated_at',
        ))
        # Serialize datetimes
        for t in tasks:
            t['created_at'] = t['created_at'].isoformat()
            t['updated_at'] = t['updated_at'].isoformat()
        return JsonResponse(tasks, safe=False)

    # POST — create task
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'invalid json'}, status=400)

    title = (data.get('title') or '').strip()
    if not title:
        return JsonResponse({'error': 'title is required'}, status=400)

    project_id = data.get('project_id')
    if not project_id:
        project = Project.objects.first()
        if not project:
            return JsonResponse({'error': 'no project exists'}, status=400)
        project_id = project.id

    task = Task.objects.create(
        project_id=project_id,
        type=data.get('type', 'dev'),
        title=title,
        description=(data.get('description') or '').strip(),
        priority=data.get('priority', 'normal'),
        created_by=data.get('created_by', ''),
        parent_id=data.get('parent_id'),
    )
    return JsonResponse({
        'id': task.id,
        'type': task.type,
        'status': task.status,
        'title': task.title,
    }, status=201)


@csrf_exempt
@_api_auth
@require_http_methods(['GET', 'PUT'])
def api_task_detail(request, task_id):
    try:
        task = Task.objects.get(id=task_id)
    except Task.DoesNotExist:
        return JsonResponse({'error': 'not found'}, status=404)

    if request.method == 'GET':
        return JsonResponse({
            'id': task.id,
            'project_id': task.project_id,
            'type': task.type,
            'title': task.title,
            'description': task.description,
            'priority': task.priority,
            'status': task.status,
            'parent_id': task.parent_id,
            'created_by': task.created_by,
            'note': task.note,
            'metadata': task.metadata,
            'created_at': task.created_at.isoformat(),
            'updated_at': task.updated_at.isoformat(),
        })

    # PUT — update task
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'invalid json'}, status=400)

    if 'status' in data:
        task.status = data['status']
    if 'note' in data:
        task.note = data['note']
    if 'metadata' in data:
        task.metadata.update(data['metadata'])
    task.save()
    return JsonResponse({'ok': True, 'id': task.id, 'status': task.status})


# ---------------------------------------------------------------------------
# Chat API
# ---------------------------------------------------------------------------

@csrf_exempt
@_api_auth
@require_http_methods(['GET', 'POST'])
def api_chat(request):
    if request.method == 'GET':
        project_id = request.GET.get('project_id')
        after = int(request.GET.get('after', 0))
        qs = ChatMessage.objects.filter(id__gt=after)
        if project_id:
            qs = qs.filter(project_id=project_id)
        msgs = list(qs.order_by('id').values(
            'id', 'project_id', 'role', 'content', 'delivered',
            'metadata', 'created_at',
        ))
        for m in msgs:
            m['created_at'] = m['created_at'].isoformat()
        return JsonResponse(msgs, safe=False)

    # POST — send message
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'invalid json'}, status=400)

    project_id = data.get('project_id')
    if not project_id:
        project = Project.objects.first()
        if not project:
            return JsonResponse({'error': 'no project exists'}, status=400)
        project_id = project.id

    role = data.get('role', 'user')
    delivered = role != 'user'  # PM messages are pre-delivered

    msg = ChatMessage.objects.create(
        project_id=project_id,
        role=role,
        content=data.get('content', ''),
        delivered=delivered,
        metadata=data.get('metadata', {}),
    )
    return JsonResponse({
        'id': msg.id,
        'role': msg.role,
        'content': msg.content,
        'created_at': msg.created_at.isoformat(),
    }, status=201)


@csrf_exempt
@_api_auth
@require_GET
def api_chat_pending(request):
    """Return oldest undelivered user message for PM channel to pick up."""
    msg = ChatMessage.objects.filter(
        role='user', delivered=False
    ).order_by('id').first()
    if not msg:
        return JsonResponse(None, safe=False)
    return JsonResponse({
        'id': msg.id,
        'project_id': msg.project_id,
        'content': msg.content,
        'created_at': msg.created_at.isoformat(),
    })


@csrf_exempt
@_api_auth
@require_POST
def api_chat_delivered(request, msg_id):
    """Mark a chat message as delivered."""
    updated = ChatMessage.objects.filter(id=msg_id, delivered=False).update(delivered=True)
    if not updated:
        return JsonResponse({'error': 'not found or already delivered'}, status=404)
    return JsonResponse({'ok': True})


async def api_chat_stream(request):
    """SSE stream of new chat messages for the browser."""
    project_id = request.GET.get('project_id')
    after = int(request.GET.get('after', 0))

    if not request.user.is_authenticated:
        return JsonResponse({'error': 'unauthorized'}, status=401)

    async def event_stream():
        last_id = after
        while True:
            messages = await sync_to_async(list)(
                ChatMessage.objects.filter(
                    project_id=project_id, id__gt=last_id
                ).order_by('id').values('id', 'role', 'content', 'created_at')
            )
            for msg in messages:
                msg['created_at'] = msg['created_at'].isoformat()
                data = json.dumps(msg)
                yield f"event: message\ndata: {data}\n\n"
                last_id = msg['id']
            await asyncio.sleep(2)

    return StreamingHttpResponse(
        event_stream(),
        content_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


# ---------------------------------------------------------------------------
# Project API
# ---------------------------------------------------------------------------

@csrf_exempt
@_api_auth
@require_http_methods(['GET', 'POST'])
def api_project(request):
    if request.method == 'GET':
        project = Project.objects.first()
        if not project:
            return JsonResponse(None, safe=False)
        return JsonResponse({
            'id': project.id,
            'name': project.name,
            'spec': project.spec,
            'status': project.status,
            'created_at': project.created_at.isoformat(),
        })

    # POST — create or update
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'invalid json'}, status=400)

    project, created = Project.objects.update_or_create(
        id=data.get('id', 1),
        defaults={
            'name': data.get('name', 'Forge Project'),
            'spec': data.get('spec', ''),
            'status': data.get('status', 'planning'),
        }
    )
    return JsonResponse({
        'id': project.id,
        'name': project.name,
        'status': project.status,
        'created': created,
    }, status=201 if created else 200)


# ---------------------------------------------------------------------------
# Status API
# ---------------------------------------------------------------------------

@csrf_exempt
@_api_auth
@require_GET
def api_status(request):
    task_counts = dict(
        Task.objects.values_list('status').annotate(count=Count('id'))
    )
    type_counts = dict(
        Task.objects.filter(status='active').values_list('type').annotate(count=Count('id'))
    )
    active_agent = None
    active_task = Task.objects.filter(status='active').first()
    if active_task:
        active_agent = active_task.type

    return JsonResponse({
        'task_counts': task_counts,
        'active_agent': active_agent,
        'active_task': {
            'id': active_task.id,
            'type': active_task.type,
            'title': active_task.title,
        } if active_task else None,
    })
