"""
Claude Code subprocess manager for Forge agents.
Manages 4 persistent `claude -p` processes (pm, dev, review, qc) that communicate
via stream-json protocol over stdin/stdout.

Adapted from the cLegal ClaudeCodeManager pattern.
"""
import json
import logging
import os
import pwd
import subprocess
import threading
import time
import uuid

import django
from django.utils import timezone

logger = logging.getLogger(__name__)

CLAUDE_BIN = os.environ.get('CLAUDE_BIN', '/usr/bin/claude')
FORGE_DIR = '/opt/forge'
WORKSPACE_DIR = '/opt/forge/workspace'
FORGE_SECRET = os.environ.get('FORGE_SECRET', 'forge-dev-secret')
HUB_URL = 'http://localhost:8100'

AGENT_TYPES = ('pm', 'dev', 'review', 'qc')
TASK_DISPATCH_INTERVAL = 3  # seconds
IDLE_NUDGE_SECONDS = 120  # nudge after 2 min idle with active task
IDLE_FORCE_SECONDS = 300  # force-fail after 5 min

NEXT_AGENT = {
    'dev': 'review',
    'review': 'qc',
    'qc': 'pm',
}


class ClaudeCodeManager:
    """
    Singleton manager for 4 Claude Code agent subprocesses.
    Each agent runs as `claude -p --stream-json` with its own stdout reader thread.
    """
    _instances = {}  # agent_type -> {process, reader_thread, stderr_thread, session_id}
    _lock = threading.Lock()

    # ── Agent lifecycle ─────────────────────────────────────────────────────

    @classmethod
    def start_agent(cls, agent_type, resume=False):
        """Spawn a claude -p subprocess for the given agent role."""
        if agent_type not in AGENT_TYPES:
            raise ValueError(f"Invalid agent type: {agent_type}")

        with cls._lock:
            if agent_type in cls._instances:
                logger.warning(f"Agent {agent_type} already running")
                return False

        from .models import AgentSession, Project

        # Get or create session record
        session, created = AgentSession.objects.get_or_create(
            agent_type=agent_type,
            defaults={'id': uuid.uuid4()}
        )

        # Link to current project if not set
        if not session.project:
            project = Project.objects.first()
            if project:
                session.project = project

        session_id = str(session.id)

        # Write per-agent MCP config
        mcp_config_path = os.path.join(FORGE_DIR, f'mcp-{agent_type}.json')
        mcp_config = {
            "mcpServers": {
                f"forge-{agent_type}": {
                    "command": "/home/forge/.bun/bin/bun",
                    "args": [
                        os.path.join(FORGE_DIR, "forge-mcp-server.ts"),
                        "--type", agent_type,
                    ],
                    "env": {
                        "FORGE_SECRET": FORGE_SECRET,
                    }
                }
            }
        }
        with open(mcp_config_path, 'w') as f:
            json.dump(mcp_config, f)

        # Build system prompt from prompt file + session context
        system_prompt = cls._build_system_prompt(agent_type, session)

        # Build command
        cmd = [
            CLAUDE_BIN, '-p',
            '--input-format', 'stream-json',
            '--output-format', 'stream-json',
            '--include-partial-messages',
            '--verbose',
            '--brief',
            '--replay-user-messages',
            '--permission-mode', 'bypassPermissions',
            '--model', 'sonnet',
            '--mcp-config', mcp_config_path,
            '--add-dir', WORKSPACE_DIR,
        ]

        if resume and session.claude_session_id:
            cmd.extend(['--resume', session.claude_session_id])
        else:
            cmd.extend(['--session-id', session_id])

        if system_prompt:
            cmd.extend(['--append-system-prompt', system_prompt])

        logger.info(f"Starting agent {agent_type} (resume={resume})")

        try:
            # Build environment for the forge user
            forge_pw = pwd.getpwnam('forge')
            forge_uid = forge_pw.pw_uid
            forge_gid = forge_pw.pw_gid
            forge_home = forge_pw.pw_dir

            env = {
                'HOME': forge_home,
                'USER': 'forge',
                'LOGNAME': 'forge',
                'PATH': f"{forge_home}/.local/bin:{forge_home}/.bun/bin:/usr/local/bin:/usr/bin:/bin",
                'SHELL': '/bin/bash',
                'FORGE_SECRET': FORGE_SECRET,
                'LANG': 'en_US.UTF-8',
            }

            # Preserve API key if set
            for key in ('ANTHROPIC_API_KEY',):
                if key in os.environ:
                    env[key] = os.environ[key]

            # QC agent needs display for browser testing
            if agent_type == 'qc':
                env['DISPLAY'] = ':1'
                browser_venv = os.path.join(FORGE_DIR, 'browser-use-venv', 'bin')
                env['PATH'] = f"{browser_venv}:{env['PATH']}"

            def _demote():
                """Drop privileges to forge user before exec."""
                os.setgid(forge_gid)
                os.setuid(forge_uid)

            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=WORKSPACE_DIR,
                env=env,
                bufsize=0,
                preexec_fn=_demote,
            )

            session.pid = process.pid
            session.status = 'starting'
            session.claude_session_id = session_id
            session.error_message = ''
            session.last_activity_at = timezone.now()
            session.save(update_fields=[
                'pid', 'status', 'claude_session_id', 'error_message',
                'last_activity_at', 'project',
            ])

            reader_thread = threading.Thread(
                target=cls._stdout_reader,
                args=(agent_type, process, session_id),
                daemon=True,
                name=f'forge-stdout-{agent_type}',
            )
            reader_thread.start()

            stderr_thread = threading.Thread(
                target=cls._stderr_reader,
                args=(agent_type, process),
                daemon=True,
                name=f'forge-stderr-{agent_type}',
            )
            stderr_thread.start()

            with cls._lock:
                cls._instances[agent_type] = {
                    'process': process,
                    'reader_thread': reader_thread,
                    'stderr_thread': stderr_thread,
                    'session_id': session_id,
                }

            logger.info(f"Agent {agent_type} started, PID={process.pid}")

            # Send wake-up message to trigger the init event
            # (claude -p in stream-json mode waits for first message before emitting init)
            # Only on fresh start — resumed sessions already have context
            if not resume:
                WAKE_MSGS = {
                    'pm': 'You are Forge PM. User messages arrive directly. Reply via chat_reply(). Wait for messages.',
                    'dev': 'You are Forge Dev. Wait for coding tasks.',
                    'review': 'You are Forge Review. Wait for review tasks.',
                    'qc': 'You are Forge QC. Wait for QC tasks.',
                }
                cls.send_message(agent_type, WAKE_MSGS.get(agent_type, 'Ready.'))
            else:
                # On resume, send a minimal ping to trigger init
                cls.send_message(agent_type, 'Resumed. Standing by.')

            return True

        except Exception as e:
            logger.error(f"Failed to start agent {agent_type}: {e}")
            session.status = 'error'
            session.error_message = str(e)
            session.save(update_fields=['status', 'error_message'])
            return False

    @classmethod
    def start_all_agents(cls):
        """Start all 4 agents."""
        for agent_type in AGENT_TYPES:
            cls.start_agent(agent_type)

    @classmethod
    def send_message(cls, agent_type, message):
        """Send a user message to an agent's stdin via stream-json protocol."""
        with cls._lock:
            instance = cls._instances.get(agent_type)

        if not instance:
            logger.error(f"No running process for agent {agent_type}")
            return False

        process = instance['process']
        if process.poll() is not None:
            logger.error(f"Agent {agent_type} process has exited")
            cls._handle_process_exit(agent_type, process.returncode)
            return False

        session_id = instance['session_id']
        input_msg = {
            "type": "user",
            "session_id": session_id,
            "message": {
                "role": "user",
                "content": message,
            },
            "parent_tool_use_id": None,
        }

        try:
            line = json.dumps(input_msg, ensure_ascii=False) + '\n'
            process.stdin.write(line.encode('utf-8'))
            process.stdin.flush()

            # Update status (don't touch messages — reader owns that)
            from .models import AgentSession
            AgentSession.objects.filter(agent_type=agent_type).update(
                status='processing',
                current_activity='',
                last_activity_at=timezone.now(),
            )

            logger.info(f"Sent message to {agent_type}: {message[:80]}...")
            return True

        except (BrokenPipeError, OSError) as e:
            logger.error(f"Failed to write to {agent_type} stdin: {e}")
            cls._handle_process_exit(agent_type, -1)
            return False

    @classmethod
    def stop_agent(cls, agent_type):
        """Stop an agent subprocess gracefully."""
        with cls._lock:
            instance = cls._instances.pop(agent_type, None)

        if not instance:
            return False

        process = instance['process']
        if process.poll() is None:
            try:
                process.stdin.close()
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            except Exception:
                process.kill()

        from .models import AgentSession
        AgentSession.objects.filter(agent_type=agent_type).update(
            status='stopped', pid=None
        )
        logger.info(f"Stopped agent {agent_type}")
        return True

    @classmethod
    def stop_all_agents(cls):
        """Stop all agents."""
        for agent_type in AGENT_TYPES:
            cls.stop_agent(agent_type)

    @classmethod
    def restart_agent(cls, agent_type):
        """Stop and restart an agent with --resume."""
        cls.stop_agent(agent_type)
        time.sleep(1)
        return cls.start_agent(agent_type, resume=True)

    @classmethod
    def is_alive(cls, agent_type):
        """Check if an agent's subprocess is still running."""
        with cls._lock:
            instance = cls._instances.get(agent_type)
        if not instance:
            return False
        return instance['process'].poll() is None

    @classmethod
    def on_task_completed(cls, agent_type):
        """Called when an agent's task is marked done/failed via the API."""
        from .models import AgentSession
        AgentSession.objects.filter(agent_type=agent_type).update(
            current_task=None,
        )

    # ── Stdout reader (one per agent) ───────────────────────────────────────

    @classmethod
    def _stdout_reader(cls, agent_type, process, session_id):
        """
        Background thread: reads stdout line by line, parses stream-json events,
        and updates the AgentSession in the database.

        Uses in-memory state to avoid DB read races. Only reads from DB at startup.
        """
        from django.db import connection
        from .models import AgentSession

        # Ensure clean DB connection for this thread
        connection.close()

        text_buffer = []
        last_save = time.time()
        SAVE_INTERVAL = 0.5

        # Load existing state from DB (for resumed sessions)
        try:
            connection.ensure_connection()
            existing = AgentSession.objects.get(agent_type=agent_type)
            messages = existing.messages if isinstance(existing.messages, list) else []
            tool_calls = existing.tool_calls if isinstance(existing.tool_calls, list) else []
            event_log = existing.event_log if isinstance(existing.event_log, list) else []
            total_turns = existing.total_turns or 0
            total_cost = existing.total_cost_usd or 0.0
            total_input_tokens = existing.total_input_tokens or 0
            total_output_tokens = existing.total_output_tokens or 0
            model_used = existing.model_used or ''
            claude_sid = existing.claude_session_id or ''
        except Exception:
            messages = []
            tool_calls = []
            event_log = []
            total_turns = 0
            total_cost = 0.0
            total_input_tokens = 0
            total_output_tokens = 0
            model_used = ''
            claude_sid = ''
        current_activity = ''

        def db_save(**fields):
            """Save fields to DB via ORM update."""
            try:
                connection.ensure_connection()
                AgentSession.objects.filter(agent_type=agent_type).update(**fields)
            except Exception as e:
                logger.error(f"DB save error for {agent_type}: {e}")
                try:
                    connection.close()
                    connection.ensure_connection()
                    AgentSession.objects.filter(agent_type=agent_type).update(**fields)
                except Exception as e2:
                    logger.error(f"DB save retry failed for {agent_type}: {e2}")

        try:
            for raw_line in iter(process.stdout.readline, b''):
                line = raw_line.decode('utf-8', errors='replace').strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = event.get('type', '')

                if event_type in ('rate_limit_event',):
                    continue

                try:
                    # ── Init event ──
                    if event_type == 'system' and event.get('subtype') == 'init':
                        claude_sid = event.get('session_id', '')
                        model_used = event.get('model', '')
                        tools_list = event.get('tools', [])
                        mcp_servers = event.get('mcp_servers', [])
                        event_log.append({
                            'type': 'init',
                            'agent': agent_type,
                            'model': model_used,
                            'session_id': claude_sid,
                            'tools_count': len(tools_list),
                            'mcp_servers': [
                                s.get('name', '') for s in mcp_servers
                                if isinstance(s, dict)
                            ],
                            'ts': timezone.now().isoformat(),
                        })
                        db_save(
                            claude_session_id=claude_sid,
                            model_used=model_used,
                            status='ready',
                            event_log=event_log,
                        )
                        logger.info(f"Agent {agent_type} init, model={model_used}")
                        continue

                    # ── User message replay ──
                    if event_type == 'user':
                        user_content = event.get('message', {}).get('content', '')
                        if isinstance(user_content, str) and user_content.strip():
                            messages.append({
                                'role': 'user',
                                'content': user_content,
                                'type': 'text',
                                'ts': timezone.now().isoformat(),
                            })
                            event_log.append({
                                'type': 'user',
                                'agent': agent_type,
                                'content': user_content[:500],
                                'ts': timezone.now().isoformat(),
                            })
                            db_save(messages=messages, event_log=event_log)
                        continue

                    # ── Streaming text deltas ──
                    if event_type == 'stream_event':
                        delta = event.get('event', {}).get('delta', {})
                        if delta.get('type') == 'text_delta':
                            text_buffer.append(delta.get('text', ''))
                            now = time.time()
                            if now - last_save >= SAVE_INTERVAL and text_buffer:
                                current_activity += ''.join(text_buffer)
                                text_buffer.clear()
                                db_save(
                                    current_activity=current_activity,
                                    status='processing',
                                    last_activity_at=timezone.now(),
                                )
                                last_save = now
                        continue

                    # ── Complete assistant message ──
                    if event_type == 'assistant':
                        # Flush text buffer
                        if text_buffer:
                            current_activity += ''.join(text_buffer)
                            text_buffer.clear()

                        msg_data = event.get('message', {})
                        content_blocks = msg_data.get('content', [])

                        text_parts = []
                        new_tool_uses = []
                        for block in content_blocks:
                            if not isinstance(block, dict):
                                continue
                            if block.get('type') == 'text':
                                text_parts.append(block.get('text', ''))
                            elif block.get('type') == 'tool_use':
                                tool_entry = {
                                    'id': block.get('id', ''),
                                    'tool': block.get('name', ''),
                                    'input': block.get('input', {}),
                                    'agent': agent_type,
                                    'ts': timezone.now().isoformat(),
                                }
                                new_tool_uses.append(tool_entry)

                                # SendUserMessage = proactive status update
                                if block.get('name') == 'SendUserMessage':
                                    user_msg = block.get('input', {}).get('message', '')
                                    if user_msg:
                                        messages.append({
                                            'role': 'activity',
                                            'content': user_msg,
                                            'type': 'send_user_message',
                                            'ts': timezone.now().isoformat(),
                                        })

                        full_text = '\n'.join(text_parts).strip()
                        if full_text:
                            messages.append({
                                'role': 'assistant',
                                'content': full_text,
                                'type': 'text',
                                'ts': timezone.now().isoformat(),
                            })

                        if new_tool_uses:
                            tool_calls.extend(new_tool_uses)
                            tool_calls = tool_calls[-15:]  # keep last 15

                        # Log events
                        event_log.append({
                            'type': 'assistant',
                            'agent': agent_type,
                            'text': full_text[:1000] if full_text else '',
                            'tool_uses': [
                                {'name': t['tool'], 'input': t['input']}
                                for t in new_tool_uses
                            ],
                            'ts': timezone.now().isoformat(),
                        })
                        for tu in new_tool_uses:
                            event_log.append({
                                'type': 'tool_use',
                                'agent': agent_type,
                                'name': tu['tool'],
                                'input': tu['input'],
                                'ts': tu['ts'],
                            })

                        current_activity = ''
                        db_save(
                            messages=messages,
                            tool_calls=tool_calls,
                            event_log=event_log,
                            current_activity='',
                            last_activity_at=timezone.now(),
                        )
                        continue

                    # ── Result (turn complete) ──
                    if event_type == 'result':
                        if text_buffer:
                            remaining = ''.join(text_buffer).strip()
                            text_buffer.clear()
                            if remaining:
                                messages.append({
                                    'role': 'assistant',
                                    'content': remaining,
                                    'type': 'text',
                                    'ts': timezone.now().isoformat(),
                                })

                        total_turns += 1
                        current_activity = ''

                        usage = event.get('usage', {})
                        turn_input = (
                            usage.get('input_tokens', 0)
                            + usage.get('cache_read_input_tokens', 0)
                        )
                        turn_output = usage.get('output_tokens', 0)
                        turn_cost = event.get('total_cost_usd', 0.0)
                        turn_duration = event.get('duration_ms', 0)

                        total_cost += turn_cost
                        total_input_tokens += turn_input
                        total_output_tokens += turn_output

                        event_log.append({
                            'type': 'result',
                            'agent': agent_type,
                            'turn': total_turns,
                            'duration_ms': turn_duration,
                            'cost_usd': turn_cost,
                            'input_tokens': turn_input,
                            'output_tokens': turn_output,
                            'cache_read': usage.get('cache_read_input_tokens', 0),
                            'stop_reason': event.get('stop_reason', ''),
                            'ts': timezone.now().isoformat(),
                        })

                        db_save(
                            status='ready',
                            current_activity='',
                            total_turns=total_turns,
                            total_cost_usd=total_cost,
                            total_input_tokens=total_input_tokens,
                            total_output_tokens=total_output_tokens,
                            last_activity_at=timezone.now(),
                            messages=messages,
                            event_log=event_log,
                        )
                        logger.info(
                            f"Agent {agent_type} turn #{total_turns}, "
                            f"cost=${turn_cost:.4f}, msgs={len(messages)}"
                        )
                        continue

                except Exception as e:
                    logger.error(
                        f"Error processing {event_type} for {agent_type}: {e}",
                        exc_info=True,
                    )
                    event_log.append({
                        'type': 'error',
                        'agent': agent_type,
                        'event_type': event_type,
                        'message': str(e)[:500],
                        'ts': timezone.now().isoformat(),
                    })
                    try:
                        db_save(event_log=event_log)
                    except Exception:
                        pass
                    continue

        except Exception as e:
            logger.error(f"stdout reader error for {agent_type}: {e}", exc_info=True)
        finally:
            connection.close()

        return_code = process.poll()
        logger.info(f"stdout reader exiting for {agent_type}, rc={return_code}")
        cls._handle_process_exit(agent_type, return_code)

    @classmethod
    def _stderr_reader(cls, agent_type, process):
        """Background thread: reads stderr for error logging."""
        try:
            for raw_line in iter(process.stderr.readline, b''):
                line = raw_line.decode('utf-8', errors='replace').strip()
                if line:
                    logger.debug(f"stderr [{agent_type}]: {line[:500]}")
        except Exception as e:
            logger.debug(f"stderr reader error for {agent_type}: {e}")

    @classmethod
    def _handle_process_exit(cls, agent_type, return_code):
        """Handle subprocess exit — update session status."""
        with cls._lock:
            cls._instances.pop(agent_type, None)

        from .models import AgentSession
        django.db.connections.close_all()
        try:
            session = AgentSession.objects.get(agent_type=agent_type)
            if return_code == 0 or return_code is None:
                session.status = 'stopped'
            else:
                session.status = 'error'
                session.error_message = f"Process exited with code {return_code}"
            session.pid = None
            session.save(update_fields=['status', 'pid', 'error_message'])
        except Exception as e:
            logger.error(f"Failed to update session on exit for {agent_type}: {e}")

    # ── System prompt builder ───────────────────────────────────────────────

    @classmethod
    def _build_system_prompt(cls, agent_type, session):
        """Build the --append-system-prompt content for an agent."""
        parts = []

        # Load the agent's prompt file
        prompt_path = os.path.join(FORGE_DIR, 'prompts', f'{agent_type}.md')
        try:
            with open(prompt_path, 'r') as f:
                parts.append(f.read())
        except FileNotFoundError:
            logger.warning(f"Prompt file not found: {prompt_path}")

        # Add session context
        parts.append('\n--- Session Context ---')
        parts.append(f'Agent type: {agent_type}')
        if session.project:
            parts.append(f'Project: {session.project.name} (ID: {session.project.id})')

        # Override channel notification references
        parts.append('\nIMPORTANT: Tasks arrive as direct messages (not channel notifications).')
        parts.append('Process one task at a time. When done, call task_update() then task_create() to hand off.')
        if agent_type == 'pm':
            parts.append('User messages arrive directly. Reply via chat_reply().')
            parts.append('Use SendUserMessage regularly to keep the user informed of your progress.')

        return '\n'.join(parts)
