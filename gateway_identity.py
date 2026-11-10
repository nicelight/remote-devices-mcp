"""Google identity for the owner UI only; MCP and tunnel credentials stay separate.

Opaque, bounded server-side browser sessions deliberately share the gateway's
single-process lifecycle. Restarting signs browsers out without changing devices.
"""
import asyncio
import base64
import hashlib
import html
import os
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from starlette.responses import RedirectResponse, Response


def escape(value):
    return html.escape(str(value), quote=True)


def page(title, content, *, status=200, script=''):
    nonce = secrets.token_urlsafe(24)
    # Local fonts/assets only: no third-party requests from credential-bearing pages.
    css = '''
:root{color-scheme:light;--ink:#17314a;--muted:#526b80;--line:#c4d9e5;--accent:#185baf}
*{box-sizing:border-box}body{margin:0;min-height:100svh;font:17px/1.6 ui-sans-serif,system-ui,sans-serif;color:var(--ink);background:#edf5fc;
background-image:radial-gradient(ellipse at 9% 12%,#bfe8fbaa,transparent 52%),radial-gradient(ellipse at 91% 30%,#e1d4fb88,transparent 48%),radial-gradient(ellipse at 50% 95%,#c6f2e188,transparent 48%)}
body:before{content:"";position:fixed;inset:0;pointer-events:none;background:linear-gradient(125deg,transparent 24%,#ffffff88 25%,transparent 55%,#ffffff66 56%,transparent 75%);z-index:-1}
main{width:min(760px,100%);margin:auto;padding:40px 20px 64px}header{display:flex;align-items:center;gap:12px;margin-bottom:28px;font-size:14px;letter-spacing:.06em}
.mark{width:30px;height:36px;background:linear-gradient(145deg,#fff,#b5e1fa 45%,#bfc8eb 46%,#f5ffff);clip-path:polygon(50% 0,100% 28%,85% 79%,50% 100%,10% 76%,0 28%)}
.panel{padding:28px;border:1px solid #fff;border-radius:24px;background:#ffffffbf;box-shadow:0 12px 50px #446b8c12,inset 0 1px #fff;backdrop-filter:blur(20px)}
h1{font-size:clamp(28px,5vw,38px);line-height:1.16;letter-spacing:-.035em;margin:12px 0 20px}h2{font-size:22px;margin:0 0 12px}p{margin:12px 0}small,.muted{color:var(--muted)}
a{color:var(--accent);text-underline-offset:4px}button,.button{display:inline-flex;align-items:center;justify-content:center;min-height:48px;padding:10px 20px;border:1px solid #174e99;border-radius:12px;background:#185baf;color:white;font:inherit;font-weight:600;cursor:pointer;text-decoration:none}
button:hover,.button:hover{background:#104b94}button.secondary{color:var(--ink);background:#ffffffb8;border-color:var(--line)}:focus-visible{outline:3px solid #396cc5;outline-offset:4px}
label{display:block;margin:20px 0 8px}input,textarea{width:100%;border:1px solid var(--line);border-radius:10px;padding:12px;background:#ffffffa8;color:var(--ink);font:inherit}textarea{font:14px/1.5 ui-monospace,monospace;resize:vertical}
code{overflow-wrap:anywhere}.code{font:700 28px/1.3 ui-monospace,monospace;letter-spacing:.09em;padding:18px;border:1px solid var(--line);border-radius:12px;text-align:center;background:#f7fbff}
.steps{display:flex;flex-wrap:wrap;gap:8px 18px;list-style:none;padding:0;font-size:13px;color:var(--muted);margin:0 0 24px}.steps li[aria-current]{color:#185baf;font-weight:700}
.device{border-top:1px solid var(--line);padding:24px 0 4px;margin-top:24px}.actions{display:flex;flex-wrap:wrap;gap:12px;margin:20px 0}.actions form{margin:0}.notice{border-left:3px solid #4d86bb;padding:8px 16px;background:#e6f2ff80;border-radius:0 10px 10px 0}.danger{border-left-color:#b34444}
.connect-guide{margin:20px 0 28px}.connect-guide img{display:block;width:100%;height:auto;border:1px solid var(--line);border-radius:12px}.connect-guide figcaption{font-size:14px;color:var(--muted);margin-top:12px}.auth-hint{display:block;margin-top:10px;font-size:18px;color:var(--ink)}
footer{margin-top:24px;font-size:14px;color:var(--muted)}[role=status]{white-space:pre-wrap}details{margin-top:16px}summary{cursor:pointer;min-height:44px;padding:8px 0}
@media(min-width:640px){main{padding-top:64px}.panel{padding:40px}}
@media(prefers-reduced-motion:no-preference){.panel{animation:appear .35s ease-out}@keyframes appear{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}}
'''
    document = f'''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)} · Remote Harness</title>
<style nonce="{nonce}">{css}</style><body><main><header><span class="mark" aria-hidden="true"></span>REMOTE HARNESS</header>
<section class="panel"><h1>{escape(title)}</h1>{content}</section><footer>Одно устройство — одно подключение. Доступ можно отозвать в любой момент.</footer></main>
{('<script nonce="'+nonce+'">'+script+'</script>') if script else ''}</body></html>'''
    return Response(document, status_code=status, media_type='text/html', headers={
        'Cache-Control': 'no-store', 'Referrer-Policy': 'strict-origin',
        'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': f"default-src 'none'; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; connect-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    })


def form_fields(csrf):
    return f'<input type="hidden" name="csrf" value="{escape(csrf)}">'


async def fields(request):
    # Small urlencoded forms; no multipart parser or extra dependency required.
    body = b''
    async for chunk in request.stream():
        body += chunk
        if len(body) > 4096:
            return {}
    return {k: v[0] for k, v in parse_qs(body.decode('utf-8', errors='replace')).items()}


class OwnerAuth:
    SESSION_COOKIE = '__Host-rhm-owner'
    LOGIN_COOKIE = '__Host-rhm-login'
    SESSION_TTL = 8 * 3600
    MAX_STATES = 500
    MAX_SESSIONS = 5000

    def __init__(self, base):
        self.base = base.rstrip('/')
        self.client_id = os.environ.get('MESH_GOOGLE_CLIENT_ID', '').strip()
        self.client_secret = os.environ.get('MESH_GOOGLE_CLIENT_SECRET', '').strip()
        self.states = {}
        self.sessions = {}
        self.exchange_slots = asyncio.Semaphore(8)

    @property
    def configured(self):
        return bool(self.client_id and self.client_secret)

    def validate_config(self):
        if bool(self.client_id) != bool(self.client_secret):
            raise RuntimeError('Set both MESH_GOOGLE_CLIENT_ID and MESH_GOOGLE_CLIENT_SECRET')
        origin = urlsplit(self.base)
        if self.configured and (origin.scheme != 'https' or not origin.hostname
                or origin.username or origin.password or origin.path or origin.query or origin.fragment):
            raise RuntimeError('Google owner login requires an HTTPS public origin without a path')
        if self.configured:
            from google.oauth2 import id_token  # Fail clearly at startup if dependency is absent.
            from google.auth.transport.requests import Request

    def purge(self):
        now = time.time()
        for store in (self.states, self.sessions):
            for key, value in list(store.items()):
                if value['expires_at'] <= now:
                    store.pop(key, None)

    def owner(self, request):
        self.purge()
        if not self.configured:
            return None
        return self.sessions.get(request.cookies.get(self.SESSION_COOKIE, ''))

    def csrf_ok(self, request, owner, data):
        supplied = data.get('csrf', '')
        origin = request.headers.get('origin')
        return bool(owner and supplied and secrets.compare_digest(str(supplied).encode('utf-8'), owner['csrf'].encode('utf-8'))
                    and (origin is None or origin == self.base))

    def login_link(self, code=''):
        return '/auth/google/login' + ('?' + urlencode({'code': code}) if code else '')

    async def login(self, request):
        if not self.configured:
            return page('Вход пока недоступен', '<p>Владелец Gateway ещё не настроил Google OAuth. Передайте ему адрес этой страницы. Административный ключ для установки не нужен.</p>', status=503)
        self.purge()
        if len(self.states) >= self.MAX_STATES:
            return page('Попробуйте чуть позже', '<p>Слишком много попыток входа. Подождите несколько минут.</p>', status=429)
        code = request.query_params.get('code', '').strip().upper()
        if code and not re.fullmatch(r'[A-Z2-9]{4}-[A-Z2-9]{4}', code):
            return page('Неверный код', '<p>Откройте ссылку из установщика.</p>', status=400)
        binding = request.cookies.get(self.LOGIN_COOKIE) or secrets.token_urlsafe(32)
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        self.states[state] = {'binding': hashlib.sha256(binding.encode()).hexdigest(), 'nonce': nonce,
            'verifier': verifier, 'next': '/pair?' + urlencode({'code': code}) if code else '/account',
            'expires_at': time.time() + 600}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        query = urlencode({'client_id': self.client_id, 'redirect_uri': self.base + '/auth/google/callback',
            'response_type': 'code', 'scope': 'openid email', 'state': state, 'nonce': nonce,
            'code_challenge': challenge, 'code_challenge_method': 'S256', 'prompt': 'select_account'})
        response = RedirectResponse('https://accounts.google.com/o/oauth2/v2/auth?' + query, status_code=303,
                                    headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        response.set_cookie(self.LOGIN_COOKIE, binding, max_age=600, secure=True, httponly=True, samesite='lax', path='/')
        return response

    def exchange(self, code, flow):
        # Fixed Google endpoints: configuration and request data cannot redirect secrets.
        import requests
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token
        with requests.Session() as transport:
            response = transport.post('https://oauth2.googleapis.com/token', data={
                'client_id': self.client_id, 'client_secret': self.client_secret, 'code': code,
                'redirect_uri': self.base + '/auth/google/callback', 'grant_type': 'authorization_code',
                'code_verifier': flow['verifier']}, timeout=15, allow_redirects=False)
            if response.status_code != 200:
                raise ValueError('Google code exchange failed')
            def fetch(url, method='GET', body=None, headers=None, **kwargs):
                return Request(session=transport)(url, method=method, body=body, headers=headers, timeout=15)
            claims = id_token.verify_oauth2_token(response.json()['id_token'], fetch, self.client_id)
        if (claims.get('aud') != self.client_id or claims.get('nonce') != flow['nonce'] or not isinstance(claims.get('sub'), str)
                or not claims['sub'] or len(claims['sub']) > 255
                or claims.get('email_verified') is not True
                or claims.get('azp', self.client_id) != self.client_id):
            raise ValueError('Invalid Google identity claims')
        return {'subject': 'google:' + claims['sub'], 'email': str(claims.get('email', ''))[:320]}

    async def callback(self, request):
        self.purge()
        state = request.query_params.get('state', '')
        flow = self.states.get(state)
        binding = request.cookies.get(self.LOGIN_COOKIE, '')
        if not self.configured or not flow or not secrets.compare_digest(
                hashlib.sha256(binding.encode()).hexdigest(), flow['binding']):
            return page('Не удалось проверить вход', '<p>Ссылка входа истекла или открыта в другом браузере. Вернитесь к установщику и откройте ссылку ещё раз.</p>', status=400)
        self.states.pop(state, None)  # One attempt, including denial and upstream failures.
        if request.query_params.get('error') or not request.query_params.get('code'):
            return page('Вход отменён', f'<p>Устройство ещё не привязано.</p><a class="button" href="{escape(flow["next"])}">Попробовать снова</a>', status=400)
        if self.exchange_slots.locked():
            return page('Попробуйте ещё раз', f'<p>Сейчас выполняется много входов.</p><a href="{escape(flow["next"])}">Вернуться к устройству</a>', status=429)
        try:
            async with self.exchange_slots:
                owner = await asyncio.to_thread(self.exchange, request.query_params['code'], flow)
        except Exception:
            # Never expose provider responses, authorization codes or tokens in logs/UI.
            return page('Вход не завершён', f'<p>Не удалось проверить ответ Google. Повторите вход. Если ошибка сохраняется, сообщите владельцу Gateway.</p><a class="button" href="{escape(flow["next"])}">Вернуться</a>', status=502)
        if len(self.sessions) >= self.MAX_SESSIONS:
            return page('Попробуйте позже', '<p>Достигнут лимит сеансов входа.</p>', status=503)
        self.sessions.pop(request.cookies.get(self.SESSION_COOKIE, ''), None)
        session = secrets.token_urlsafe(32)
        self.sessions[session] = {**owner, 'csrf': secrets.token_urlsafe(32), 'expires_at': time.time() + self.SESSION_TTL}
        response = RedirectResponse(flow['next'], status_code=303, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        response.set_cookie(self.SESSION_COOKIE, session, max_age=self.SESSION_TTL, secure=True, httponly=True, samesite='lax', path='/')
        response.delete_cookie(self.LOGIN_COOKIE, path='/', secure=True, httponly=True, samesite='lax')
        return response

    async def logout(self, request):
        owner = self.owner(request)
        data = await fields(request)
        if not self.csrf_ok(request, owner, data):
            return page('Действие отклонено', '<p>Обновите страницу и повторите действие.</p>', status=403)
        self.sessions.pop(request.cookies.get(self.SESSION_COOKIE, ''), None)
        code = data.get('return_code', '')
        target = '/pair?' + urlencode({'code': code}) if re.fullmatch(r'[A-Z2-9]{4}-[A-Z2-9]{4}', code) else '/account'
        response = RedirectResponse(target, status_code=303, headers={'Cache-Control': 'no-store'})
        response.delete_cookie(self.SESSION_COOKIE, path='/', secure=True, httponly=True, samesite='lax')
        return response
