"""
Management command to start all Forge agents and the task dispatcher.
Usage: python manage.py start_forge
"""
import signal
import threading

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Start all 4 Forge Claude Code agents and the task dispatcher'

    def handle(self, *args, **options):
        from core.claude_manager import ClaudeCodeManager

        self.stdout.write('Starting Forge agents...')

        # Start all agents + dispatcher
        ClaudeCodeManager.start_all_agents()

        self.stdout.write(self.style.SUCCESS(
            'All agents started. Press Ctrl+C to stop.'
        ))

        # Block until interrupted
        stop_event = threading.Event()

        def shutdown(signum, frame):
            self.stdout.write('\nStopping all agents...')
            ClaudeCodeManager.stop_all_agents()
            self.stdout.write(self.style.SUCCESS('All agents stopped.'))
            stop_event.set()

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        stop_event.wait()
