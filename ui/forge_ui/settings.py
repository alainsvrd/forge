import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'forge-dev-insecure-key-change-in-production'
)

DEBUG = os.environ.get('FORGE_DEBUG', '1') == '1'

ALLOWED_HOSTS = ['*']

# CSRF trusted origins — set FORGE_DOMAIN in .env at deploy time
# e.g. FORGE_DOMAIN=myproject.borealhost.ai
_forge_domain = os.environ.get('FORGE_DOMAIN', '')
CSRF_TRUSTED_ORIGINS = ['http://localhost:8100', 'https://localhost:8100']
if _forge_domain:
    CSRF_TRUSTED_ORIGINS += [
        f'https://{_forge_domain}',
        f'http://{_forge_domain}',
        f'https://forge.{_forge_domain}',
        f'http://forge.{_forge_domain}',
    ]
    # Also trust the parent domain if it's a subdomain (e.g. borealhost.ai)
    parts = _forge_domain.split('.')
    if len(parts) > 2:
        parent = '.'.join(parts[-2:])
        CSRF_TRUSTED_ORIGINS += [
            f'https://*.{parent}',
            f'http://*.{parent}',
        ]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'core',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'forge_ui.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'forge_ui.wsgi.application'

# Database — PostgreSQL via unix socket
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('FORGE_DB_NAME', 'forge'),
        'USER': os.environ.get('FORGE_DB_USER', 'forge'),
        'PASSWORD': os.environ.get('FORGE_DB_PASSWORD', ''),
        'HOST': os.environ.get('FORGE_DB_HOST', ''),
        'PORT': '',
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'

# Forge internal API secret — MCP channels use this to authenticate
FORGE_SECRET = os.environ.get('FORGE_SECRET', 'forge-dev-secret')
