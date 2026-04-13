import asyncio
import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import JsonResponse, HttpResponse, StreamingHttpResponse
from django.shortcuts import render, get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from asgiref.sync import sync_to_async

import os

from .models import Project, Task, ChatMessage, AgentLog, AgentSession, Prototype, PrototypeComment


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


# ---------------------------------------------------------------------------
# Prototype Mode
# ---------------------------------------------------------------------------

WORKSPACE_DIR = '/opt/forge/workspace'

@login_required
def prototype_view(request):
    """Prototype viewer page."""
    return render(request, 'core/prototype.html')


@login_required
def prototype_preview(request, prototype_id):
    """Serve prototype HTML with comment overlay injected."""
    proto = get_object_or_404(Prototype, id=prototype_id)
    html_path = proto.html_path or 'prototype/index.html'
    full_path = os.path.join(WORKSPACE_DIR, html_path)

    if not os.path.isfile(full_path):
        return HttpResponse(
            f'<h2>Prototype not built yet</h2><p>Expected: {html_path}</p>',
            content_type='text/html',
        )

    with open(full_path, 'r') as f:
        html = f.read()

    # Inject comment overlay script before </body>
    overlay_script = _build_comment_overlay(prototype_id)
    if '</body>' in html:
        html = html.replace('</body>', overlay_script + '\n</body>')
    else:
        html += overlay_script

    return HttpResponse(html, content_type='text/html')


def _build_comment_overlay(prototype_id):
    """Build the inline JS comment overlay for prototype previews."""
    return f'''
<style>
  .forge-comment-fab {{
    position: fixed; bottom: 24px; right: 24px; z-index: 99999;
    width: 52px; height: 52px; border-radius: 50%;
    background: #6c8cff; color: #fff; border: none; cursor: pointer;
    font-size: 22px; box-shadow: 0 4px 16px rgba(0,0,0,0.3);
    display: flex; align-items: center; justify-content: center;
    transition: transform 0.15s, background 0.15s;
  }}
  .forge-comment-fab:hover {{ transform: scale(1.1); }}
  .forge-comment-fab.active {{ background: #f87171; }}
  .forge-comment-mode * {{ cursor: crosshair !important; }}
  .forge-comment-mode *:hover {{ outline: 2px solid #6c8cff !important; outline-offset: 2px; }}
  .forge-comment-popup {{
    position: fixed; z-index: 100000;
    background: #1a1d27; border: 1px solid #2e3347; border-radius: 10px;
    padding: 14px; width: 320px; box-shadow: 0 8px 32px rgba(0,0,0,0.5);
    font-family: -apple-system, sans-serif;
  }}
  .forge-comment-popup textarea {{
    width: 100%; min-height: 70px; background: #242836; border: 1px solid #2e3347;
    border-radius: 6px; color: #e1e4ed; padding: 8px; font-size: 13px;
    font-family: inherit; resize: vertical;
  }}
  .forge-comment-popup textarea:focus {{ outline: none; border-color: #6c8cff; }}
  .forge-comment-popup .forge-btn {{
    margin-top: 8px; padding: 6px 16px; border-radius: 6px; border: none;
    background: #6c8cff; color: #fff; cursor: pointer; font-size: 13px;
  }}
  .forge-comment-popup .forge-btn:hover {{ opacity: 0.9; }}
  .forge-comment-popup .forge-cancel {{
    background: transparent; color: #8b90a5; border: 1px solid #2e3347; margin-left: 6px;
  }}
  .forge-comment-popup .forge-el-ref {{
    font-size: 11px; color: #8b90a5; margin-bottom: 8px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }}
  .forge-pin {{
    position: absolute; z-index: 99998; width: 22px; height: 22px;
    background: #fb923c; border-radius: 50%; border: 2px solid #fff;
    cursor: pointer; font-size: 11px; color: #fff; display: flex;
    align-items: center; justify-content: center; font-weight: 700;
    box-shadow: 0 2px 8px rgba(0,0,0,0.3); transition: transform 0.1s;
  }}
  .forge-pin:hover {{ transform: scale(1.2); }}
  .forge-pin.resolved {{ background: #4ade80; opacity: 0.6; }}
  .forge-pin-tooltip {{
    position: absolute; bottom: 28px; left: -4px; z-index: 100001;
    background: #1a1d27; border: 1px solid #2e3347; border-radius: 8px;
    padding: 10px; width: 260px; font-size: 12px; color: #e1e4ed;
    box-shadow: 0 4px 16px rgba(0,0,0,0.4); display: none;
    font-family: -apple-system, sans-serif;
  }}
  .forge-pin:hover .forge-pin-tooltip {{ display: block; }}
</style>

<button class="forge-comment-fab" id="forgeCommentFab" title="Toggle comment mode">&#128172;</button>

<script>
(function() {{
  const PROTO_ID = {prototype_id};
  const API = '/api/prototypes/' + PROTO_ID + '/comments/';
  let commentMode = false;
  const fab = document.getElementById('forgeCommentFab');

  fab.addEventListener('click', () => {{
    commentMode = !commentMode;
    fab.classList.toggle('active', commentMode);
    document.body.classList.toggle('forge-comment-mode', commentMode);
  }});

  function getSelector(el) {{
    if (el.id) return '#' + el.id;
    let path = [];
    while (el && el !== document.body) {{
      let tag = el.tagName.toLowerCase();
      if (el.className && typeof el.className === 'string') {{
        const cls = el.className.split(/\\s+/).filter(c => !c.startsWith('forge-')).slice(0,2).join('.');
        if (cls) tag += '.' + cls;
      }}
      const parent = el.parentElement;
      if (parent) {{
        const siblings = Array.from(parent.children).filter(c => c.tagName === el.tagName);
        if (siblings.length > 1) tag += ':nth-child(' + (Array.from(parent.children).indexOf(el) + 1) + ')';
      }}
      path.unshift(tag);
      el = parent;
    }}
    return path.join(' > ');
  }}

  document.addEventListener('click', (e) => {{
    if (!commentMode) return;
    if (e.target.closest('.forge-comment-popup') || e.target.closest('.forge-comment-fab') || e.target.closest('.forge-pin')) return;
    e.preventDefault();
    e.stopPropagation();

    // Remove existing popup
    document.querySelectorAll('.forge-comment-popup').forEach(p => p.remove());

    const el = e.target;
    const selector = getSelector(el);
    const elText = (el.textContent || '').trim().substring(0, 100);
    const rect = el.getBoundingClientRect();

    const popup = document.createElement('div');
    popup.className = 'forge-comment-popup';
    popup.style.top = Math.min(rect.bottom + 8, window.innerHeight - 200) + 'px';
    popup.style.left = Math.min(rect.left, window.innerWidth - 340) + 'px';
    popup.innerHTML = `
      <div class="forge-el-ref">On: ${{elText || selector}}</div>
      <textarea placeholder="Your feedback on this element..." autofocus></textarea>
      <button class="forge-btn" id="forgeSubmitComment">Submit</button>
      <button class="forge-btn forge-cancel" id="forgeCancelComment">Cancel</button>
    `;
    document.body.appendChild(popup);
    popup.querySelector('textarea').focus();

    popup.querySelector('#forgeCancelComment').onclick = () => popup.remove();
    popup.querySelector('#forgeSubmitComment').onclick = async () => {{
      const content = popup.querySelector('textarea').value.trim();
      if (!content) return;
      try {{
        await fetch(API, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ content, element_selector: selector, element_text: elText, author: 'user' }}),
        }});
        popup.innerHTML = '<div style="color:#4ade80;padding:8px;">Comment saved!</div>';
        setTimeout(() => {{ popup.remove(); loadComments(); }}, 1000);
      }} catch(err) {{
        popup.innerHTML = '<div style="color:#f87171;padding:8px;">Error saving comment</div>';
      }}
    }};
  }}, true);

  // Load existing comments as pins
  async function loadComments() {{
    document.querySelectorAll('.forge-pin').forEach(p => p.remove());
    try {{
      const res = await fetch(API);
      const data = await res.json();
      (data.comments || []).forEach((c, i) => {{
        if (!c.element_selector) return;
        const target = document.querySelector(c.element_selector);
        if (!target) return;
        const rect = target.getBoundingClientRect();
        const pin = document.createElement('div');
        pin.className = 'forge-pin' + (c.resolved ? ' resolved' : '');
        pin.style.top = (window.scrollY + rect.top - 8) + 'px';
        pin.style.left = (rect.right - 8) + 'px';
        pin.textContent = i + 1;
        pin.innerHTML += '<div class="forge-pin-tooltip"><strong>' + c.author + ':</strong> ' +
          c.content.replace(/</g, '&lt;') + '</div>';
        document.body.appendChild(pin);
      }});
    }} catch(e) {{}}
  }}
  setTimeout(loadComments, 500);
}})();
</script>'''


@csrf_exempt
@_api_auth
@require_http_methods(['GET', 'POST'])
def api_prototypes(request):
    """List or create prototypes."""
    if request.method == 'GET':
        qs = Prototype.objects.all()
        status = request.GET.get('status')
        if status:
            qs = qs.filter(status=status)
        protos = list(qs.values(
            'id', 'project_id', 'title', 'description', 'status',
            'html_path', 'backend_spec', 'created_at', 'updated_at',
        ))
        for p in protos:
            p['created_at'] = p['created_at'].isoformat()
            p['updated_at'] = p['updated_at'].isoformat()
            p['comment_count'] = PrototypeComment.objects.filter(
                prototype_id=p['id'], resolved=False
            ).count()
        return JsonResponse(protos, safe=False)

    # POST
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'invalid json'}, status=400)

    project = Project.objects.first()
    if not project:
        return JsonResponse({'error': 'no project'}, status=400)

    proto = Prototype.objects.create(
        project=project,
        title=data.get('title', ''),
        description=data.get('description', ''),
        html_path=data.get('html_path', 'prototype/index.html'),
        backend_spec=data.get('backend_spec', ''),
    )
    return JsonResponse({
        'id': proto.id,
        'title': proto.title,
        'status': proto.status,
    }, status=201)


@csrf_exempt
@_api_auth
@require_http_methods(['GET', 'PUT'])
def api_prototype_detail(request, prototype_id):
    """Get or update a prototype."""
    proto = get_object_or_404(Prototype, id=prototype_id)

    if request.method == 'GET':
        comments = list(PrototypeComment.objects.filter(
            prototype=proto
        ).values('id', 'author', 'content', 'element_selector', 'element_text',
                 'resolved', 'created_at'))
        for c in comments:
            c['created_at'] = c['created_at'].isoformat()
        return JsonResponse({
            'id': proto.id,
            'title': proto.title,
            'description': proto.description,
            'status': proto.status,
            'html_path': proto.html_path,
            'backend_spec': proto.backend_spec,
            'comments': comments,
            'created_at': proto.created_at.isoformat(),
        })

    # PUT
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'invalid json'}, status=400)

    if 'status' in data:
        proto.status = data['status']
    if 'backend_spec' in data:
        proto.backend_spec = data['backend_spec']
    if 'html_path' in data:
        proto.html_path = data['html_path']
    proto.save()
    return JsonResponse({'ok': True, 'id': proto.id, 'status': proto.status})


@csrf_exempt
@_api_auth
@require_http_methods(['GET', 'POST'])
def api_prototype_comments(request, prototype_id):
    """List or add comments for a prototype."""
    proto = get_object_or_404(Prototype, id=prototype_id)

    if request.method == 'GET':
        comments = list(PrototypeComment.objects.filter(
            prototype=proto
        ).values('id', 'author', 'content', 'element_selector', 'element_text',
                 'resolved', 'created_at'))
        for c in comments:
            c['created_at'] = c['created_at'].isoformat()
        return JsonResponse({'comments': comments})

    # POST
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'invalid json'}, status=400)

    comment = PrototypeComment.objects.create(
        prototype=proto,
        author=data.get('author', 'user'),
        content=data.get('content', ''),
        element_selector=data.get('element_selector', ''),
        element_text=data.get('element_text', ''),
    )
    return JsonResponse({
        'id': comment.id,
        'author': comment.author,
        'content': comment.content,
    }, status=201)


@csrf_exempt
@_api_auth
@require_http_methods(['PUT'])
def api_prototype_comment_resolve(request, prototype_id, comment_id):
    """Resolve a comment."""
    comment = get_object_or_404(PrototypeComment, id=comment_id, prototype_id=prototype_id)
    comment.resolved = True
    comment.save(update_fields=['resolved'])
    return JsonResponse({'ok': True})
