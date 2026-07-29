# Arxivist — документация для разработчиков

Актуальна на 2026-07

---

## 1. Что это

Arxivist — веб-приложение «AI-ассистент в чате»: поиск/скачивание статей arXiv, саммаризация, загрузка документов, генерация HTML-дашбордов, обогащение внешней базы знаний (ArangoDB-граф), поиск вакансий/компаний/кандидатов в HeadHunter с сохранением выбранных кандидатов в базу знаний.

Ядро — **Claude Agent SDK** (`claude_agent_sdk`): агентный цикл `query()` с in-process MCP-серверами.

## 2. Стек и протоколы

| Слой | Технология | Где |
|---|---|---|
| Backend | Python 3.11, Flask, gunicorn (sync) | `app.py`, `gunicorn.conf.py` |
| Агент | Claude Agent SDK (`query`, `ClaudeAgentOptions`, `tool`, `create_sdk_mcp_server`) | `app.py`, `arxiv_tools.py`, `tools/` |
| Инструменты агента | MCP (Model Context Protocol), in-process серверы | `tools/*.py`, `arxiv_tools.py` |
| Стриминг статусов | SSE (Server-Sent Events) браузер ← Flask | `/chat` в `app.py`, `templates/index.html` |
| Транспорт к модели | HTTP-бридж со своим SSE (direct → api.anthropic.com; proxy → upstream 202+SSE) | `bridge/bridge.py` |
| Авторизация HH | OAuth2: `client_credentials` (app-токен) + `authorization_code`/`refresh_token` (user-токен), CSRF `state` | `tools/hh_auth.py`, роуты `/hh/*` |
| База знаний | REST поверх ArangoDB-графа, JSON-only ingestion, async-задачи с поллингом | `tools/kb_tools.py` |
| Frontend | Vanilla JS, marked (markdown), DOMPurify (санитизация), SSE-чтение через `fetch` reader | `templates/index.html` |
| Деплой | Docker Compose, nginx на хосте | `Dockerfile`, `docker-compose.yml`, `entrypoint.sh` |

## 3. Архитектура

```
Браузер ──POST /chat (SSE)──► Flask (gunicorn, 2 sync-воркера)
   │                             │
   │◄──status/result события─────┤ run_chat_stream() — async-генератор
   │                             ▼
   │                       claude_agent_sdk.query()
   │                             │  ANTHROPIC_BASE_URL=http://127.0.0.1:9999
   │                             ▼
   │                       bridge/bridge.py ──direct──► api.anthropic.com
   │                             └────────────proxy───► upstream (202+SSE)
   │
   │        Агент вызывает MCP-инструменты (in-process):
   │        ┌─────────┬──────────┬───────────┬─────────┬─────────┐
   │        │  arxiv  │   api    │ dashboard │   kb    │   hh    │
   │        └────┬────┴────┬─────┴─────┬─────┴────┬────┴────┬────┘
   │             ▼         ▼           ▼          ▼         ▼
   │        export.arxiv  любой     dashboards/  neo.rndl  api.hh.ru
   │          .org        REST API  registry     .ru:5001
   │
   └──GET /hh/authorize ──► hh.ru OAuth ──► GET /hh/callback (обмен кода, токен в config/)
```

Два уровня возможностей агента (см. CLAUDE.md «Two-tier capability injection»):
- **Типизированные MCP-инструменты** — для частых детерминированных вызовов (KB, HH, arXiv, дашборды). Форма запроса зашита в Python, модель не может её «пере-изобрести».
- **Generic `call_api` + SKILL.md** — для новых/редких API. Знание «как вызывать» живёт в тексте скилла; агент сам пишет новые скиллы (`creating-skills`) — механизм саморасширения.

## 4. Карта модулей

### agent_pipeline.py — ЕДИНЫЙ агентский пайплайн
Извлечён из app.py, чтобы чат и API никогда не разъезжались. Содержит: статус-фразы + `detect_lang`, `build_chat_prompt`, `build_chat_options` (системный промпт + 5 MCP-серверов + allowed_tools), `run_chat_stream`, `resolve_attached_files`, `summarize_papers`, `run_search_pipeline` (arXiv: агент-фаза + саммари, никогда не бросает) и главный **`pipeline_events(message, history, session_id, attached_file_ids)`** — sync-генератор событий `{"type":"status"|"result",…}`. Три потребителя: `/chat` (оборачивает в SSE), `job_runner.py` (пишет в файлы джобы), `/api/v1/arxiv/search`.

### api_v1.py — blueprint `/api/v1` (бэкенд-модуль)
Все внешние API-эндпоинты + `before_request`-хук аутентификации по ключу (`X-API-Key` / `Authorization: Bearer`) против `tools/api_keys.py`. Открыты без ключа только `/docs`, `/openapi.yaml`, `/health`. Конверт ошибок `{"error":{code,message,…}}`; 403 `hh_user_authorization_required` несёт `authorize_url`. Опциональный CORS через env `API_CORS_ORIGINS`. Типизированные HH-роуты зовут `hh_tools._get()` (сырой JSON api.hh.ru), KB-роуты — `kb_tools._request()/_poll_task()`; `POST /candidates/save` — детерминированный конвейер резюме→документ→база.

### jobs_store.py + job_runner.py — асинхронные агентские джобы
202+poll без Celery/Redis: `jobs/<job_id>/{request.json,status.json,events.ndjson,runner.log}`. `create_job` → Popen `job_runner.py <id>` (отдельный процесс — свой `_session`, воркер gunicorn не занят). Состояния `queued→running→succeeded|failed|canceled`; RMW статуса под per-job flock; self-heal мёртвых раннеров (проверка pid при чтении); отмена SIGTERM'ом; TTL-уборка (JOB_TTL_DAYS=7); лимит ёмкости `active_count()` против `MAX_CONCURRENT_AGENT_JOBS` (429). Watchdog в раннере: `JOB_TIMEOUT` (900с) через SIGALRM.

### tools/api_keys.py + scripts/apikeys.py — API-ключи
Формат `axv_<id8hex>_<secret ~256бит>`; в `config/api_keys.json` только sha256 всего ключа (утёкший бэкап config/ не раскрывает ключи). Валидация — чтение файла на каждый запрос (как hh_auth) + `hmac.compare_digest`; записи под flock (как dashboards_store). Отзыв действует немедленно, БЕЗ рестарта (рестарт убил бы минутные джобы). `last_used_at` обновляется с троттлингом 5 мин. CLI: `create --label` / `list` / `revoke <id>`.

### app.py — HTTP-слой и оркестрация агента
| Что | Детали |
|---|---|
| `_PrefixStripMiddleware` | срезает `APP_BASE` из PATH_INFO — те же роуты работают и напрямую, и за nginx |
| `summarize_papers()` | прямой (не агентный) вызов `anthropic.messages.create` по собранным абстрактам; строгий markdown-шаблон |
| `run_agent()` | агентная фаза `/search`: только поиск+скачивание, без саммари |
| `build_chat_prompt()` | история диалога реплеится в промпт каждого запроса (серверного хранилища истории нет) |
| `_STATUS_PHRASES`, `_detect_lang()`, `_status_phrase()` | статус-фразы по имени инструмента, ru/en по кириллице в сообщении |
| `_build_chat_options()` | системный промпт (правила Output style / Decision rules) + 5 MCP-серверов + allowed_tools + SESSION CONTEXT (session_id, активный дашборд, приложенные файлы) |
| `run_chat_stream()` | async-генератор: yield `{"type":"status"}` на каждый tool call, `{"type":"final"}` — последний текст-без-инструментов |
| `/chat` | sync-обёртка: свой event loop на запрос (без потоков!), SSE-ответ, выбор ответа: дашборд > саммари статей > финальный текст |
| `/hh/authorize, /hh/callback, /hh/status` | OAuth-флоу HH (см. §5.3) |
| `/api/proxy-mode` | прокси к `POST/GET :9999/mode` бриджа |
| прочее | `/`, `/search`, `/files`, `/upload`, `/d/<uuid>`, `/dashboards` |

### arxiv_tools.py — MCP-сервер `arxiv` + сессия
- `search_arxiv` — переписывает запрос: минус стоп-слова, каждому терму `all:`, соединение `AND`; преф-термы (`au:`, `cat:`) не трогает. Retry с экспоненциальным бэкоффом.
- `download_paper`, `list_downloads`.
- **Модульное состояние `_session`** (papers/downloaded/search_query/errors) + `reset_session()`/`get_session()` — фундамент модели воркеров (§10).
- `_record_error(stage, detail)` — ошибки инструментов не летят исключениями в роут, а собираются и форматируются в ответ пользователю. **Новые инструменты должны следовать этому паттерну.**

### tools/hh_auth.py — токены HeadHunter
- `TOKEN_FILE = config/hh_token.json` (bind-mount, переживает рестарты; в .gitignore).
- `get_app_token()` — `client_credentials`; на ответ HH «app token refresh too early» возвращает уже сохранённый живой токен.
- `exchange_code()` / `_refresh()` — user-токен и его обновление.
- `load_access_token()` — главная точка: валидный сохранённый → refresh → авто-минт app-токена (если заданы `HH_CLIENT_ID/SECRET`) → `HH_ACCESS_TOKEN` из env. Читается на каждый запрос — токен, полученный посреди сессии, подхватывается без рестарта.
- `new_state()` / `consume_state()` — одноразовый CSRF `state` (файл `config/hh_oauth_state.json`, TTL 10 мин; файловый, потому что authorize и callback могут попасть в разные gunicorn-воркеры).
- `token_status()` — для `/hh/status` (его же поллит фронтенд для авто-продолжения).

### tools/hh_tools.py — MCP-сервер `hh`
- `_headers()` — Bearer-токен свежим на каждый вызов; `_get()` — retry 429, **self-heal 403**: если использовался app-токен — перевыпустить и повторить один раз (user-токен никогда не затирается).
- Инструменты: `search_hh_vacancies`, `get_hh_vacancy`, `search_hh_employers`, `get_hh_employer_details`, `get_hh_reference` (areas/professional_roles/dictionaries/skills), `search_hh_resumes`, `get_hh_resume`.
- `search_hh_resumes` — расширенные опциональные фильтры (проверены живьём против api.hh.ru): `salary_from/to` (+авто `currency=RUR`), `schedule`, `employment`, `education_level`, `age_from/to`, `relocation` (требует `area` — без него фильтр снимается с заметкой в выдаче), `job_search_status`, `period`, `label`, `order_by`. Конвенция: фильтр ставится ТОЛЬКО при явном требовании пользователя/документа; мягкие критерии — в `text` (закреплено в описании инструмента и SKILL.md).
- `search_hh_resumes` без user-токена возвращает `AUTHORIZATION_REQUIRED` + markdown-ссылку на `/hh/authorize`; агент обязан показать её и остановиться (правило в системном промпте).
- Матрица доступа HH (проверено живьём): `/areas`, `/dictionaries`, `/professional_roles` — публичные; `/vacancies*`, `/employers*` — нужен любой токен (app достаточно); `/resumes*` — только user-токен + платная подписка на базу резюме.

### tools/kb_tools.py — MCP-сервер `kb`
- Auth: заголовок `X-API-Key` (не `Server-API-Key` — тот даёт 401).
- `query_knowledge_base` → `POST /api/qa/ask`, при `task_id` поллит `GET /api/tasks/{id}`.
- `add_document_to_kb` — **единственный путь записи, JSON-only** (сырой текст сервер отвергает): base64(JSON) → если база существует `POST /api/server/database/add-file`, иначе `files/upload` + `database/create-from-files`; поллит `GET /api/server/tasks/{id}`. Автосоздание базы.
- Конвенции баз: `companies` — данные о компаниях, `candidates` — кандидаты (один документ = профиль + `resume_url`).

### tools/call_api.py — MCP-сервер `api`
Generic HTTP-инструмент (GET/POST/PUT/PATCH/DELETE, headers, JSON body). **Никакой API-специфики в коде by design** — всё в SKILL.md-файлах.

### tools/dashboard_tools.py + dashboards_store.py
- `create_dashboard(html, session_id)` — валидация против design-system.md (DOCTYPE, `<style>`, `<!-- === SECTION: === -->` маркеры), запись `dashboards/<uuid>/index.html`, регистрация.
- `dashboards_store` — реестр `dashboards/registry.json` под `fcntl.flock` (два воркера могут гоняться). POSIX-only.
- `extract_text(path)` — docx/md/txt; PDF читаются встроенным `Read` (нативный vision-парсинг Claude).

### uploads_store.py / text_extract.py
Валидация загрузок (pdf/docx/md/txt, ≤25 МБ), запись `uploads/<file_id>/original.<ext>` + `text.md` (кроме PDF) + `meta.json` с `parse_mode: pdf-native|extracted|plain`.

### bridge/bridge.py — транспорт к модели
- Слушает `127.0.0.1:9999`, SDK всегда ходит через него (`ANTHROPIC_BASE_URL`).
- **direct**: добавляет `x-api-key`, форвардит на api.anthropic.com. **proxy**: POST → upstream (202+task_id) → GET stream URL → собирает/транслирует SSE (умеет и канонический Anthropic-формат, и обёртку прокси).
- `POST /mode` — переключение на лету; сохраняется в `config/bridge_mode`. `GET /health`.

### templates/index.html — фронтенд (один файл)
- SSE-чтение `/chat` через `resp.body.getReader()` + ручной парсинг `\n\n`.
- `session_id` — UUID в `localStorage`, шлётся с каждым `/chat`.
- Рендер ответов: `DOMPurify.sanitize(marked.parse(...))`; ссылки `/d/`, `/hh/`, `http*` — в новую вкладку.
- **Авто-продолжение после OAuth HH** (см. §5.3): `startHHWatch()` взводится, когда в ответе агента есть `/hh/authorize`.
- Тогглер Direct/Proxy в шапке → `/api/proxy-mode`.

## 5. Потоки запросов

### 5.1 `/chat` (основной)
```
POST /chat {message, history, session_id, attached_file_ids}
 ├─ reset_session(); резолв активного дашборда и файлов
 ├─ SSE: {"type":"status","phrase":"Думаю…"}
 ├─ агентный цикл: на каждый tool_use → {"type":"status","phrase":<по _STATUS_PHRASES>}
 ├─ выбор ответа: дашборд-текст > summarize_papers() (если качались статьи) > финальный текст > отчёт об ошибках
 └─ SSE: {"type":"result","reply","files","search_query","elapsed_seconds"}
```
Требование к nginx: `proxy_buffering off` для этой локации (роут дополнительно шлёт `X-Accel-Buffering: no`).

### 5.2 `/search` (структурный, две фазы)
1. Агент (только arxiv-инструменты) ищет и скачивает. 2. `summarize_papers()` — детерминированное саммари прямым API-вызовом. Ответ — обычный JSON (не SSE).

### 5.3 OAuth HeadHunter + авто-продолжение
```
Пользователь: «найди кандидатов…»
 └─ search_hh_resumes → нет user-токена → AUTHORIZATION_REQUIRED + ссылка
     └─ агент показывает [Войти через HeadHunter](/arxivist/hh/authorize)
         фронтенд видит '/hh/authorize' в ответе → startHHWatch()
             │  (поллинг GET /hh/status каждые 3с, до 10 мин)
Пользователь кликает → /hh/authorize: new_state() → redirect на hh.ru
Пользователь логинится → hh.ru → /hh/callback?code&state
 ├─ consume_state() (CSRF, одноразовый) → exchange_code() → токен в config/hh_token.json
 └─ страница «✓ подключён»: BroadcastChannel('arxivist-hh-auth') + window.close()
Чат-вкладка: сигнал канала ИЛИ /hh/status показал user-токен
 └─ авто-отправка «Я авторизовался в HeadHunter — продолжай.» → агент продолжает поиск
```
Два сигнала не случайно: BroadcastChannel работает только same-origin (5050 vs 5000 — разные origin), поллинг покрывает все случаи. Токен обновляется по `refresh_token` автоматически (~14 дней жизни access), повторный вход нужен только при отзыве.

### 5.4 Кандидаты → база знаний
`search_hh_resumes` возвращает нумерованный список со ссылками → агент спрашивает, кого сохранить (никогда не сохраняет сам — персональные данные) → на выбранных: `get_hh_resume` (обогащение) → `add_document_to_kb(database_name="candidates", document={candidate, resume_id, resume_url, area, experience_years, key_skills, salary, education, last_position, source, saved_at})`. Схема — в `.claude/skills/searching-headhunter/SKILL.md`.

### 5.5 Дашборды и загрузки
`POST /upload` → `uploads/<file_id>/` → id уходит в `/chat` → сервер кладёт read_path/parse_mode в SESSION CONTEXT системного промпта → агент читает (PDF — встроенным `Read`) → `create_dashboard` → `/d/<uuid>`. Редактирование — `Edit` по якорям-маркерам без смены UUID (скилл `editing-dashboards`).

### 5.6 API-модуль: агентская джоба (202 + poll)
```
POST /api/v1/agent/jobs {message, history?, session_id?, attached_file_ids?}
 ├─ auth: X-API-Key → tools/api_keys.verify_key
 ├─ active_count() >= MAX_CONCURRENT_AGENT_JOBS → 429 + Retry-After
 ├─ jobs_store.create_job() → Popen(job_runner.py <id>, start_new_session)
 └─ 202 {job_id, poll}
job_runner (отдельный процесс):
 ├─ state=running(pid) → pipeline_events(...)  ← ТОТ ЖЕ код, что /chat
 ├─ status-фразы → events.ndjson
 └─ result → status.json (state=succeeded; cancel не перезаписывается)
GET /api/v1/agent/jobs/{id} → {state, progress[], result{reply,files,…}, error}
DELETE /api/v1/agent/jobs/{id} → SIGTERM раннеру + state=canceled
```
Клиентский цикл: 202 → poll каждые 2–5 c → `succeeded` → `result.reply` (markdown). Файл вакансии: сначала `POST /api/v1/files` → `file_id` → в `attached_file_ids`.

## 6. HTTP API

| Метод/путь | Назначение | Ответ |
|---|---|---|
| `GET /` | UI | HTML |
| `POST /chat` | чат-агент | **SSE**: `status`* → `result` |
| `POST /search` | структурный поиск arXiv | JSON `{result, files, search_query, elapsed_seconds}` |
| `POST /upload` | загрузка файлов (multipart, поле `file`) | JSON `{files:[meta], errors:[]}` |
| `GET /files` | все скачанные PDF | JSON |
| `GET /d/<uuid>` | дашборд | HTML |
| `GET /dashboards` | список дашбордов | JSON |
| `GET/POST /api/proxy-mode` | режим бриджа | JSON `{mode, proxy_available, direct_available}` |
| `GET /hh/authorize` | старт OAuth (мятет state) | 302 на hh.ru |
| `GET /hh/callback` | обмен кода на токен (CSRF-проверка) | HTML-страница с авто-закрытием |
| `GET /hh/status` | статус токена | JSON `{configured, grant, has_refresh_token, expires_in_seconds, …}` |

**API-модуль `/api/v1` (по API-ключу; полный контракт — `docs/openapi.yaml`, Swagger UI — `GET /api/v1/docs`):**

| Метод/путь | Назначение |
|---|---|
| `POST /api/v1/agent/jobs` → `GET/DELETE /api/v1/agent/jobs/{id}` | асинхронные агентские задачи (полный флоу) |
| `POST /api/v1/files`, `GET /api/v1/files/{name}` | загрузка документов / скачивание PDF |
| `GET /api/v1/hh/vacancies[/{id}]`, `/hh/employers[/{id}]`, `/hh/resumes[/{id}]`, `/hh/reference/{type}`, `/hh/auth/status` | типизированный HH (сырой JSON) |
| `GET /api/v1/kb/databases`, `POST /api/v1/kb/query`, `POST /api/v1/kb/documents`, `GET /api/v1/kb/tasks/{id}` | база знаний |
| `POST /api/v1/candidates/save` | резюме → структурированный профиль → база `candidates` |
| `POST /api/v1/arxiv/search` | структурный поиск статей (sync, 1–3 мин) |
| `GET /api/v1/dashboards`, `GET /api/v1/health`, `GET /api/v1/docs`, `GET /api/v1/openapi.yaml` | сервисные |

Примеры:
```bash
# Чат (SSE)
curl -N -X POST http://localhost:5000/chat -H 'Content-Type: application/json' \
  -d '{"message":"найди вакансии python в Москве","history":[],"session_id":"dev-test","attached_file_ids":[]}'

# Статус HH-токена
curl -s http://localhost:5000/hh/status | jq

# Переключить режим бриджа
curl -s -X POST http://localhost:5000/api/proxy-mode -H 'Content-Type: application/json' -d '{"mode":"direct"}'
```

## 7. MCP-инструменты (имена для allowed_tools: `mcp__<server>__<tool>`)

| Сервер | Инструменты |
|---|---|
| `arxiv` | search_arxiv, download_paper, list_downloads |
| `api` | call_api |
| `dashboard` | create_dashboard, extract_text |
| `kb` | list_kb_databases, query_knowledge_base, add_document_to_kb, get_kb_task_status |
| `hh` | search_hh_vacancies, get_hh_vacancy, search_hh_employers, get_hh_employer_details, get_hh_reference, search_hh_resumes, get_hh_resume |

Плюс встроенные: `Skill, Read, Write, Edit, Bash, WebSearch, WebFetch`.

## 8. Скиллы (`.claude/skills/`)

Скилл = папка с `SKILL.md` (front-matter `name`/`description` + инструкции). Загружаются через `setting_sources=["user","project"]` + разрешённый инструмент `Skill`.

| Скилл | Роль |
|---|---|
| searching-arxiv, summarizing-papers | оркестрация arXiv-флоу |
| creating-dashboards, editing-dashboards, reading-uploads | дашборд-флоу (+ design-system.md) |
| ingesting-to-knowledge-base, enriching-knowledge-base | KB: справка по инструментам; веб-рисёрч → JSON → ingestion |
| searching-headhunter | HH-флоу, схема документа кандидата, матрица доступа |
| creating-skills | **саморасширение**: агент пишет новый SKILL.md для незнакомого API, затем зовёт его через `call_api` |

Правило «код vs скилл»: частый детерминированный вызов → типизированный Python-инструмент; оркестрация/новые API → скилл. Docker-том `arxivist_skills` сохраняет скиллы, созданные агентом на рантайме.

## 9. Переменные окружения (.env)

| Переменная | Обязательность | Что делает |
|---|---|---|
| `ANTHROPIC_API_KEY` | direct-режим | ключ API |
| `ANTHROPIC_BASE_URL` | да | `http://127.0.0.1:9999` — всегда бридж |
| `PROXY_UPSTREAM_URL`, `PROXY_AUTH_TOKEN`, `PROXY_STREAM_URL_TEMPLATE` | proxy-режим | upstream-прокси (template содержит `{task_id}`) |
| `APP_BASE` | за nginx | префикс путей (`/arxivist`) |
| `KNOWLEDGE_BASE_URL`, `KNOWLEDGE_BASE_API_KEY` | для KB | `http://neo.rndl.ru:5001` + ключ (заголовок X-API-Key) |
| `HH_CLIENT_ID`, `HH_CLIENT_SECRET` | для HH-поиска | app-токен минтится автоматически |
| `HH_REDIRECT_URI` | опционально | пиновка redirect_uri; **на проде обязательно сменить с localhost** |
| `HH_ACCESS_TOKEN` | опционально | готовый токен (fallback) |
| `SUMMARIZE_MODEL` | опционально | модель для summarize_papers |
| `BRIDGE_*` | опционально | таймауты/порт/лог/файл режима бриджа |
| `MAX_CONCURRENT_AGENT_JOBS` | опционально (2) | ёмкость одновременных агентских джоб API |
| `JOB_TIMEOUT` | опционально (900) | watchdog раннера, сек |
| `JOB_TTL_DAYS` | опционально (7) | срок хранения завершённых джоб |
| `API_CORS_ORIGINS` | опционально (выкл) | CORS для /api/v1: `*` или список origin'ов |

Рантайм-состояние в `config/` (bind-mount, в .gitignore): `hh_token.json` (секрет!), `api_keys.json` (только хэши), `hh_oauth_state.json`, `bridge_mode`. Джобы — в `jobs/` (bind-mount, в .gitignore).

## 10. Состояние и конкурентность — ГЛАВНОЕ ограничение

- `arxiv_tools._session` — **модульный dict, не потокобезопасен**. Изоляция — по процессам.
- Процессная модель: 2 sync-воркера gunicorn (быстрые HTTP-запросы + дебаг-чат) + **по одному процессу-раннеру на API-джобу** (агентские запуски не занимают воркеры) + процесс бриджа. `preload_app=False`, `timeout=600`.
- `/chat` и раннер крутят async-генератор через **свежий event loop на процесс/запрос, без потоков**.
- Межпроцессные гонки закрыты `fcntl.flock`: реестр дашбордов, статусы джоб (per-job), хранилище API-ключей, **обновление HH-токена** (HH ротирует refresh_token при использовании — двойной refresh из воркера и раннера убил бы токен).
- **Нельзя** добавлять потоки/async-воркеры/preload, не вынеся сессию из модульного скоупа. История чата живёт на клиенте и реплеится в промпт — сервер stateless между запросами (кроме файлов/токенов/реестров/джоб).

## 11. Деплой

```bash
docker compose up --build -d     # прод-подобный запуск
python app.py                    # локальный dev (Flask, :5000)
```
- Порты: контейнер `5050` → хост `127.0.0.1:5050` и `127.0.0.1:5000` (второй — чтобы работал HH-callback `http://localhost:5000/hh/callback` при локальном тесте).
- `entrypoint.sh`: бридж (фон, health-wait) → gunicorn; смерть любого процесса валит контейнер (restart policy поднимет).
- nginx (хост): `location /arxivist/ { proxy_pass http://127.0.0.1:5050/; proxy_buffering off; client_max_body_size 25m; }` + стандартные X-Forwarded-* (в приложении `ProxyFix(x_for=1, x_proto=1, x_host=1)` — ровно один доверенный прокси).
- Тома: `./downloads`, `./.claude/skills`, `./uploads`, `./dashboards`, `./config`.

## 12. Как расширять

**Новый типизированный MCP-инструмент:**
1. Модуль в `tools/`, функции с `@tool(name, description, json_schema)`, возврат `{"content":[{"type":"text","text":...}]}`.
2. `create_sdk_mcp_server(name="x", tools=[...])`.
3. В `app.py`: сервер в `mcp_servers`, имена в `allowed_tools` (`mcp__x__tool`), при необходимости — правило в системный промпт.
4. Статус-фраза в `_STATUS_PHRASES` (ru+en) — иначе пользователь увидит генерик «Обрабатываю…».
5. Ошибки: возвращайте текстом и/или `_record_error()` — не бросайте исключение в роут.

**Новый скилл:** папка в `.claude/skills/` с `SKILL.md` (front-matter + инструкции). Перечислить в списке скиллов системного промпта, если он должен подсказываться.

**Новая внешняя интеграция «на лету»:** ничего не делать — пользователь описывает API в чате, агент сам пишет скилл (`creating-skills`) и ходит через `call_api`.

## 13. Известные грабли

- **Root-owned каталоги** после Docker bind-mount: локальный запуск падает `PermissionError` на `dashboards/.registry.lock` → `sudo chown -R $USER` или работать в Docker.
- **nginx-буферизация убивает SSE** — обязательно `proxy_buffering off` для `/chat`.
- KB: только JSON-ingestion; QA-поллинг — `/api/tasks/{id}` (без `/server/`), файловые операции — `/api/server/tasks/{id}`.
- HH: `/specializations` мёртв (404) — использовать `professional_roles`; поиск требует токен; «app token refresh too early» — норма (токен ещё жив).
- `HH_REDIRECT_URI` с localhost на проде молча ломает вход — обязательно менять.
- PDF не извлекаются сервером — их читает `Read` (vision); `extract_text` для PDF намеренно отказывает.
- Agent SDK буферизует JSON-RPC сообщения: `max_buffer_size` поднят до 64 МиБ (PDF как base64-блок).
- arXiv индексирует только английский — запросы переводятся до вызова инструмента (правило в промптах).
