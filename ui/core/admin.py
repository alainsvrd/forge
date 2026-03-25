from django.contrib import admin
from .models import Project, Task, ChatMessage, AgentLog


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ['name', 'status', 'created_at']


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ['id', 'type', 'title', 'status', 'priority', 'created_by', 'created_at']
    list_filter = ['type', 'status', 'priority']


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ['id', 'project', 'role', 'delivered', 'created_at']
    list_filter = ['role', 'delivered']


@admin.register(AgentLog)
class AgentLogAdmin(admin.ModelAdmin):
    list_display = ['id', 'agent_type', 'event', 'task', 'created_at']
    list_filter = ['agent_type', 'event']
