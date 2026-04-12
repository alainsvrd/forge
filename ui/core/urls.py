from django.urls import path
from . import views

urlpatterns = [
    # UI views
    path('', views.dashboard_view, name='dashboard'),
    path('chat/', views.pm_chat_view, name='pm_chat'),
    path('activity/', views.mcp_activity_view, name='mcp_activity'),

    # Task API (MCP server + browser)
    path('api/tasks/', views.api_tasks, name='api_tasks'),
    path('api/tasks/<int:task_id>/', views.api_task_detail, name='api_task_detail'),

    # Chat API (PM agent + browser)
    path('api/chat/', views.api_chat, name='api_chat'),
    path('api/chat/pending/', views.api_chat_pending, name='api_chat_pending'),
    path('api/chat/<int:msg_id>/delivered/', views.api_chat_delivered, name='api_chat_delivered'),
    path('api/chat/stream/', views.api_chat_stream, name='api_chat_stream'),

    # Project API
    path('api/project/', views.api_project, name='api_project'),

    # Status API (legacy, kept for compatibility)
    path('api/status/', views.api_status, name='api_status'),

    # Context API (MCP server calls this for context)
    path('api/context/', views.api_context, name='api_context'),

    # Agent monitoring API (new)
    path('api/agents/status/', views.api_agents_status, name='api_agents_status'),
    path('api/agents/<str:agent_type>/', views.api_agent_detail, name='api_agent_detail'),
    path('api/agents/<str:agent_type>/start/', views.api_agent_start, name='api_agent_start'),
    path('api/agents/<str:agent_type>/stop/', views.api_agent_stop, name='api_agent_stop'),
    path('api/agents/<str:agent_type>/nudge/', views.api_agent_nudge, name='api_agent_nudge'),

    # MCP activity API
    path('api/mcp-activity/', views.api_mcp_activity, name='api_mcp_activity'),
]
