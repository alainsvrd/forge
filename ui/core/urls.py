from django.urls import path
from . import views

urlpatterns = [
    # UI views
    path('', views.dashboard_view, name='dashboard'),
    path('chat/', views.pm_chat_view, name='pm_chat'),

    # Task API (MCP channels + browser)
    path('api/tasks/', views.api_tasks, name='api_tasks'),
    path('api/tasks/<int:task_id>/', views.api_task_detail, name='api_task_detail'),

    # Chat API (PM channel + browser)
    path('api/chat/', views.api_chat, name='api_chat'),
    path('api/chat/pending/', views.api_chat_pending, name='api_chat_pending'),
    path('api/chat/<int:msg_id>/delivered/', views.api_chat_delivered, name='api_chat_delivered'),
    path('api/chat/stream/', views.api_chat_stream, name='api_chat_stream'),

    # Project API
    path('api/project/', views.api_project, name='api_project'),

    # Status API
    path('api/status/', views.api_status, name='api_status'),

    # Context API (MCP channels fetch this before delivering notifications)
    path('api/context/', views.api_context, name='api_context'),
]
