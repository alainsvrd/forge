import uuid

from django.db import models


class Project(models.Model):
    name = models.CharField(max_length=200)
    spec = models.TextField(default='')
    status = models.CharField(max_length=20, default='planning')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class Task(models.Model):
    TYPE_CHOICES = [
        ('pm', 'PM'),
        ('dev', 'Dev'),
        ('review', 'Review'),
        ('qc', 'QC'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('active', 'Active'),
        ('done', 'Done'),
        ('failed', 'Failed'),
    ]
    PRIORITY_CHOICES = [
        ('low', 'Low'),
        ('normal', 'Normal'),
        ('high', 'High'),
        ('urgent', 'Urgent'),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='tasks')
    type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    title = models.CharField(max_length=500)
    description = models.TextField(default='')
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='normal')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='children')
    created_by = models.CharField(max_length=10, default='')
    note = models.TextField(default='')
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['type', 'status']),
            models.Index(fields=['status']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'[{self.type}/{self.status}] {self.title}'


class ChatMessage(models.Model):
    ROLE_CHOICES = [
        ('user', 'User'),
        ('pm', 'PM'),
        ('system', 'System'),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='messages')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    content = models.TextField()
    delivered = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['project', 'created_at']),
        ]
        ordering = ['created_at']

    def __str__(self):
        return f'[{self.role}] {self.content[:80]}'


class AgentLog(models.Model):
    agent_type = models.CharField(max_length=10)
    event = models.CharField(max_length=50)
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.SET_NULL)
    detail = models.TextField(default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['agent_type', 'created_at']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'[{self.agent_type}] {self.event}'


class AgentSession(models.Model):
    """Tracks a persistent claude -p subprocess for one agent role."""
    TYPE_CHOICES = [
        ('pm', 'PM'),
        ('dev', 'Dev'),
        ('review', 'Review'),
        ('qc', 'QC'),
    ]
    STATUS_CHOICES = [
        ('stopped', 'Stopped'),
        ('starting', 'Starting'),
        ('ready', 'Ready'),
        ('processing', 'Processing'),
        ('error', 'Error'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent_type = models.CharField(max_length=10, choices=TYPE_CHOICES, unique=True)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='agent_sessions')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='stopped')
    claude_session_id = models.CharField(max_length=100, blank=True)
    pid = models.IntegerField(null=True, blank=True)

    # Conversation (single-writer: only stdout reader thread mutates these)
    messages = models.JSONField(default=list)
    current_activity = models.TextField(default='', blank=True)
    tool_calls = models.JSONField(default=list)
    event_log = models.JSONField(default=list)

    # Current task in-flight
    current_task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='agent_session')

    # Stats
    total_turns = models.IntegerField(default=0)
    total_cost_usd = models.FloatField(default=0.0)
    total_input_tokens = models.IntegerField(default=0)
    total_output_tokens = models.IntegerField(default=0)
    model_used = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(default='', blank=True)
    last_activity_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['agent_type']),
        ]

    def __str__(self):
        return f'AgentSession {self.agent_type} ({self.status})'
