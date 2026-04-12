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

from .models import Project, Task, ChatMessage, AgentLog, AgentSession


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

    # Auto-deliver: push the task to the target agent immediately
    from .claude_manager import ClaudeCodeManager
    agent_type = task.type
    alive = ClaudeCodeManager.is_alive(agent_type)
    import logging
    logger = logging.getLogger(__name__)
    logger.warning(f"Auto-deliver task #{task.id} to {agent_type}: alive={alive}")
    if alive:
        parts = [
            f'=== TASK #{task.id} ===',
            f'Title: {task.title}',
            f'Priority: {task.priority}',
            f'Created by: {task.created_by}',
            f'\nDescription:\n{task.description}',
        ]
        if task.parent_id:
            chain = _build_task_parent_chain(task.parent_id)
            if chain:
                parts.append('\n=== PARENT TASK CHAIN ===')
                for t in chain:
                    note_preview = (t['note'] or '')[:300]
                    parts.append(
                        f"#{t['id']} [{t['type']}/{t['status']}] {t['title']}"
                        + (f"\n  Note: {note_preview}" if note_preview else "")
                    )
        parts.append(
            f'\n\nProcess this task. When done, call task_update(task_id={task.id}, '
            f'status="done"|"failed", note="...") then task_create() to hand off.'
        )
        message = '\n'.join(parts)
        sent = ClaudeCodeManager.send_message(agent_type, message)
        logger.warning(f"  send_message result: {sent}")
        if sent:
            task.status = 'active'
            task.save(update_fields=['status'])
            AgentSession.objects.filter(agent_type=agent_type).update(current_task=task)

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

    old_status = task.status
    if 'status' in data:
        task.status = data['status']
    if 'note' in data:
        task.note = data['note']
    if 'metadata' in data:
        task.metadata.update(data['metadata'])
    task.save()

    # Notify ClaudeCodeManager when a task completes
    if task.status in ('done', 'failed') and old_status == 'active':
        try:
            from .claude_manager import ClaudeCodeManager
            ClaudeCodeManager.on_task_completed(task.type)
        except Exception:
            pass  # Manager may not be running

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

    # Direct delivery to PM — no dispatcher needed for chat
    if role == 'user':
        try:
            from .claude_manager import ClaudeCodeManager
            if ClaudeCodeManager.is_alive('pm'):
                ClaudeCodeManager.send_message('pm', msg.content)
                msg.delivered = True
                msg.save(update_fields=['delivered'])
        except Exception:
            pass  # Dispatcher will pick it up as fallback

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

    is_authed = await sync_to_async(lambda: request.user.is_authenticated)()
    if not is_authed:
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


# ---------------------------------------------------------------------------
# Context API (MCP channels call this before delivering notifications)
# ---------------------------------------------------------------------------

def _build_task_parent_chain(task_id, max_depth=5):
    """Walk up the parent chain for a task, return list of summaries."""
    chain = []
    seen = set()
    current_id = task_id
    while current_id and len(chain) < max_depth:
        if current_id in seen:
            break
        seen.add(current_id)
        try:
            t = Task.objects.get(id=current_id)
            chain.append({
                'id': t.id, 'type': t.type, 'status': t.status,
                'title': t.title, 'note': t.note[:300] if t.note else '',
            })
            current_id = t.parent_id
        except Task.DoesNotExist:
            break
    chain.reverse()
    return chain


@csrf_exempt
@_api_auth
@require_GET
def api_context(request):
    """Build a context bundle for an agent before delivering a notification.

    GET /api/context/?type=pm
      → chat history + task summary (for PM chat delivery)

    GET /api/context/?type=dev&task_id=5
      → task detail + parent chain (for task delivery)
    """
    agent_type = request.GET.get('type', 'pm')
    task_id = request.GET.get('task_id')

    if agent_type == 'pm':
        # PM context: chat history + task summary
        chat_msgs = list(
            ChatMessage.objects.order_by('-id')[:15].values(
                'id', 'role', 'content', 'created_at'
            )
        )
        chat_msgs.reverse()
        for m in chat_msgs:
            m['created_at'] = m['created_at'].isoformat()
            m['content'] = m['content'][:500]  # Truncate for context window

        tasks = list(
            Task.objects.exclude(status='failed').order_by('-id')[:10].values(
                'id', 'type', 'status', 'title', 'note', 'created_by'
            )
        )
        tasks.reverse()
        for t in tasks:
            t['note'] = (t['note'] or '')[:200]

        return JsonResponse({
            'chat_history': chat_msgs,
            'task_summary': tasks,
        })

    else:
        # Dev/Review/QC context: task + parent chain
        if not task_id:
            return JsonResponse({'error': 'task_id required'}, status=400)

        try:
            task = Task.objects.get(id=task_id)
        except Task.DoesNotExist:
            return JsonResponse({'error': 'task not found'}, status=404)

        chain = _build_task_parent_chain(task.parent_id) if task.parent_id else []

        return JsonResponse({
            'task': {
                'id': task.id, 'type': task.type, 'status': task.status,
                'title': task.title, 'description': task.description,
                'priority': task.priority, 'created_by': task.created_by,
                'note': task.note, 'parent_id': task.parent_id,
            },
            'parent_chain': chain,
        })


# ---------------------------------------------------------------------------
# Agent monitoring API
# ---------------------------------------------------------------------------

@csrf_exempt
@_api_auth
@require_GET
def api_agents_status(request):
    """Compact status for all 4 agents — primary polling target for the dashboard."""
    from .claude_manager import ClaudeCodeManager

    agents = {}
    for session in AgentSession.objects.all():
        current_task_data = None
        if session.current_task:
            ct = session.current_task
            current_task_data = {
                'id': ct.id, 'title': ct.title, 'type': ct.type,
                'created_at': ct.created_at.isoformat(),
            }

        tool_calls_list = session.tool_calls if isinstance(session.tool_calls, list) else []

        agents[session.agent_type] = {
            'status': session.status,
            'current_activity': session.current_activity or '',
            'current_task': current_task_data,
            'tool_calls': tool_calls_list[-5:],
            'total_cost_usd': session.total_cost_usd,
            'total_turns': session.total_turns,
            'total_input_tokens': session.total_input_tokens,
            'total_output_tokens': session.total_output_tokens,
            'model_used': session.model_used,
            'message_count': len(session.messages) if isinstance(session.messages, list) else 0,
            'is_alive': ClaudeCodeManager.is_alive(session.agent_type),
            'last_activity_at': session.last_activity_at.isoformat() if session.last_activity_at else None,
            'error_message': session.error_message,
        }

    # Task counts
    task_counts = dict(
        Task.objects.values_list('status').annotate(count=Count('id'))
    )
    active_task = Task.objects.filter(status='active').first()

    return JsonResponse({
        'agents': agents,
        'task_counts': task_counts,
        'active_task': {
            'id': active_task.id,
            'type': active_task.type,
            'title': active_task.title,
        } if active_task else None,
    })


@csrf_exempt
@_api_auth
@require_GET
def api_agent_detail(request, agent_type):
    """Full detail for one agent including messages and event_log."""
    try:
        session = AgentSession.objects.get(agent_type=agent_type)
    except AgentSession.DoesNotExist:
        return JsonResponse({'error': 'agent not found'}, status=404)

    since_msg = int(request.GET.get('since_msg', 0))
    messages = session.messages if isinstance(session.messages, list) else []
    display_messages = []
    for msg in messages[since_msg:]:
        role = msg.get('role', '')
        content = msg.get('content', '')
        if role in ('user', 'assistant', 'activity') and content:
            display_messages.append(msg)

    return JsonResponse({
        'agent_type': session.agent_type,
        'status': session.status,
        'current_activity': session.current_activity or '',
        'messages': display_messages,
        'total_messages': len(messages),
        'tool_calls': session.tool_calls if isinstance(session.tool_calls, list) else [],
        'total_turns': session.total_turns,
        'total_cost_usd': session.total_cost_usd,
        'total_input_tokens': session.total_input_tokens,
        'total_output_tokens': session.total_output_tokens,
        'model_used': session.model_used,
        'error_message': session.error_message,
    })


@csrf_exempt
@_api_auth
@require_POST
def api_agent_start(request, agent_type):
    """Start or restart an agent."""
    from .claude_manager import ClaudeCodeManager
    if agent_type not in ('pm', 'dev', 'review', 'qc'):
        return JsonResponse({'error': 'invalid agent type'}, status=400)

    if ClaudeCodeManager.is_alive(agent_type):
        ClaudeCodeManager.restart_agent(agent_type)
    else:
        ClaudeCodeManager.start_agent(agent_type, resume=True)

    return JsonResponse({'ok': True, 'agent_type': agent_type, 'action': 'started'})


@csrf_exempt
@_api_auth
@require_POST
def api_agent_stop(request, agent_type):
    """Stop an agent."""
    from .claude_manager import ClaudeCodeManager
    if agent_type not in ('pm', 'dev', 'review', 'qc'):
        return JsonResponse({'error': 'invalid agent type'}, status=400)
    ClaudeCodeManager.stop_agent(agent_type)
    return JsonResponse({'ok': True, 'agent_type': agent_type, 'action': 'stopped'})


@csrf_exempt
@_api_auth
@require_POST
def api_agent_nudge(request, agent_type):
    """Send a message to an agent (used by PM to coordinate)."""
    from .claude_manager import ClaudeCodeManager
    if agent_type not in ('pm', 'dev', 'review', 'qc'):
        return JsonResponse({'error': 'invalid agent type'}, status=400)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'invalid json'}, status=400)
    message = data.get('message', '')
    if not message:
        return JsonResponse({'error': 'message required'}, status=400)
    if ClaudeCodeManager.send_message(agent_type, message):
        return JsonResponse({'ok': True, 'agent_type': agent_type})
    return JsonResponse({'error': 'agent not alive'}, status=400)


@csrf_exempt
@_api_auth
@require_GET
def api_mcp_activity(request):
    """Aggregated tool_calls from all agents, sorted chronologically."""
    since = request.GET.get('since', '')
    limit = int(request.GET.get('limit', 50))

    all_calls = []
    for session in AgentSession.objects.all():
        calls = session.tool_calls if isinstance(session.tool_calls, list) else []
        for call in calls:
            if since and call.get('ts', '') <= since:
                continue
            call_copy = dict(call)
            call_copy.setdefault('agent', session.agent_type)
            all_calls.append(call_copy)

    # Sort by timestamp
    all_calls.sort(key=lambda c: c.get('ts', ''))
    all_calls = all_calls[-limit:]

    return JsonResponse({'activity': all_calls})


@login_required
def mcp_activity_view(request):
    """MCP activity page."""
    return render(request, 'core/mcp_activity.html')
