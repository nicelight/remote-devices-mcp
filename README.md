# Remote Harness MCP — подключите свои устройства к ChatGPT

> Установите агент, войдите через Google и управляйте своим компьютером через отдельное MCP-подключение.

[Главная и установка](README.md) | [🇷🇺 Русская версия](README_RU.md)

---

## 🚀 Подключите своё устройство к ChatGPT

**Установка → вход через Google → подтверждение устройства → готовая ссылка.**

Это руководство для нашей версии Remote Harness MCP. Запускайте установщик на
компьютере, которым хотите управлять. Административный ключ не нужен, вводить код
вручную тоже не придётся.

**Клиентом становится хост, на котором запущен установщик.** На нём устанавливается
клиентский агент, который подключается к существующему серверу Gateway.
Заранее выбранных клиентов в репозитории нет: имя нового узла определяется по
текущему устройству, а уже установленный агент сохраняет свою привязку.
Установка на другой хост и развёртывание нового Gateway — отдельные явные действия.

### 1 · Установите агент

На **Ubuntu / Linux** откройте терминал обычного пользователя и выполните:

```bash
curl -fSL --connect-timeout 10 --max-time 60 \
  https://mcp.natureonzoom.win/install.sh \
  -o /tmp/rhm-install.sh &&
bash /tmp/rhm-install.sh --pair --quick --lang=ru
```

Установщик показывает загрузку компонентов и текущий этап. При долгой операции
появляются сообщения с прошедшим временем. Если понадобятся системные пакеты,
он может запросить пароль для `sudo`.

<details>
<summary>Установка на Windows через PowerShell</summary>

Откройте PowerShell от своего пользователя:

```powershell
$installerPath = Join-Path $env:TEMP 'rhm-install.ps1'
Invoke-WebRequest -Uri 'https://mcp.natureonzoom.win/install.ps1' -OutFile $installerPath
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installerPath -Pair -Quick -Lang ru
```

Нативный сценарий Windows ещё ожидает полного прогона. Для первого подключения
рекомендуем использовать Linux, на котором проводилась установка.

</details>

### 2 · Подтвердите устройство в браузере

Установщик спросит: **«Приступить к авторизации через браузер?»**
Нажмите **Enter**, чтобы открыть страницу подключения. Ответы `нет`, `no`, `n`
(в любом регистре) оставят браузер закрытым — ссылку можно открыть вручную.
Любой другой ответ также откроет браузер.

На странице подключения:

1. Нажмите **«Войти через Google»** и выберите свой аккаунт.
2. Проверьте имя компьютера.
3. Нажмите **«Подключить устройство»** и вернитесь в терминал.

**Код вводить не нужно.** Он уже передаётся в ссылке; при желании его можно сверить
с терминалом. Если браузер не открылся, скопируйте ссылку из установщика и откройте
её вручную, в том числе на телефоне. Заявка действует **10 минут**.

> Подключайте только свои устройства: агент выполняет команды и работает с файлами
> с правами пользователя, который его запустил.

### 3 · Дождитесь проверки связи

После подтверждения установщик сохранит индивидуальные ключи, настроит запуск
агента и запросит реальный ответ устройства через MCP. Дождитесь сообщения
**«Готово: устройство отвечает»** и ссылки **Server URL**.

Открытый кабинет или успешный вход в Google сами по себе ещё не означают,
что агент подключился. Окончание установки подтверждает именно ответ устройства.

### 4 · Добавьте подключение в ChatGPT

При создании MCP-подключения укажите:

| Поле | Значение |
| --- | --- |
| Название | Понятное имя текущего устройства |
| Server URL | Полная приватная ссылка из установщика или кабинета |
| Authentication | **No authentication** |

Google подтверждает владельца в кабинете, а доступ к MCP проверяется отдельным
ключом в ссылке. **Не публикуйте Server URL:** тот, у кого есть эта ссылка,
получает доступ к устройству.

Для первой проверки попросите ChatGPT, выбрав это подключение:

> «Вызови mesh_status и покажи имя подключённого компьютера».

**Один компьютер — один коннектор.** Для следующего устройства повторите установку
на нём и добавьте отдельное подключение в ChatGPT.

### Ваши устройства всегда под рукой

Откройте **[кабинет Remote Harness](https://mcp.natureonzoom.win/account)**,
чтобы скопировать ссылку, проверить связь или управлять доступом:

- **Заменить ссылку** — старый Server URL перестанет работать. Вставьте новый в ChatGPT.
- **Отозвать доступ** — отключит агент и его коннекторы. Для повторного подключения потребуется новая привязка.

Кабинет показывает только устройства вашего Google-аккаунта. Устройства, ранее
созданные администратором, автоматически в аккаунт не переносятся.

### Если что-то пошло не так

| Что вы видите | Что сделать |
| --- | --- |
| `Pairing code expired` | Запустите установщик снова той же командой. Обновлённая версия заменяет истёкшую сохранённую заявку автоматически. |
| Браузер не открылся | Откройте готовую ссылку из терминала вручную. |
| Google не разрешает вход | Проверьте выбранный аккаунт. Пока OAuth-приложение в режиме Testing, его владелец должен добавить ваш email в Test users. |
| Страница сообщает, что заявка потеряна или истекла | Повторите установку. Перезапуск Gateway также сбрасывает незавершённые заявки. |
| Устройство не отвечает после подтверждения | Проверьте интернет и дождитесь завершения запуска агента. На Linux посмотрите `systemctl --user status rhm-agent.service`. |
| Устройство уже настроено | Установщик повторно использует сохранённую конфигурацию. Не удаляйте `agent.env` ради обычного повторного запуска. |

На Linux локальная конфигурация обычно находится в `~/.config/remote-harness-mcp/`,
а исходники установленного агента — в `~/.remote-harness-mcp/`. Конфигурация содержит
ключи; при обращении за помощью достаточно текста ошибки без токенов и приватных ссылок.

Для Android/Termux есть [отдельная инструкция](docs/TERMUX.md). На некоторых
прошивках приложение замораживается в фоне; одна установка агента это ограничение не устраняет.

Для владельца сервера: [настройка Google OAuth и устройство авторизации](docs/GOOGLE_OWNER_ONBOARDING.md).

---

> **Historical upstream documentation.** The sections below describe upstream capabilities and installation variants. For this managed Gateway, use the Russian onboarding guide above; its installers preserve our modified version.

## 💡 The idea

Gemini in a browser or on a phone can talk, but it cannot *do* anything on your computer.
**Remote Harness MCP** closes that gap. It is a free, open-source **Model Context Protocol (MCP)** server that you connect to **Gemini Spark** ([gemini.google.com/customize/connectors](https://gemini.google.com/customize/connectors) or the Gemini mobile app) with a single link. After that, Gemini gets hands:

- runs shell commands, builds and tests code, manages git;
- reads, writes and edits files;
- searches the file system;
- starts long-running jobs and checks on them later;
- watches CPU, RAM and disk.

```text
 📱 Gemini app / 🌐 gemini.google.com (Spark)
              │  MCP (SSE / Streamable HTTP)
              ▼
     ☁️  Gateway  racknerd-5a24bf9.merino-carob.ts.net   ← relay only, TLS
              │  outbound WebSocket tunnel (no open ports)
              ▼
   💻 Your PC · laptop · VPS · home server  →  executes the task
```

**Result:** your phone becomes a remote control for every machine you own — and Gemini is the agent that operates them.

---

## 🔥 What you can do with it

Just ask Gemini in plain language, from anywhere:

- *"Check why my home server is slow and show the top processes"*
- *"Pull the latest changes in ~/projects/api, run the tests and tell me what failed"*
- *"Fix the typo in config.yaml and restart the service"*
- *"Start a full backup in the background and tell me when it's done"*
- *"How much free disk space is left on my laptop?"*
- *"Turn on the phone's torch, and tell me its battery level and signal"*

No SSH client, no terminal on your phone, no VPN — only a chat.

---

## 📸 In action

Three exchanges from one chat, each answered by a different machine on the other
side of the bridge. The phone never opened a terminal.

**The laptop describes itself** — *"@Rem Matebook-deb, give me the system and hardware specs"*:

![Gemini listing a laptop's CPU, GPU, disks and memory, read from the node itself](docs/screenshots/01-hardware.png)

**The phone reports its own battery, radio and VPN** — *"@Rem Phone, check"*.
The SSID and both addresses are mosaicked; everything else came from `device_info`:

![Gemini reporting battery, Wi-Fi signal, LTE status, VPN and CPU temperature of an Android phone](docs/screenshots/02-phone.png)

**The desktop is photographed, published and read back** — *"take a screenshot of @RemMatebook-deb"*,
then *"describe what is running on the screens"*:

![Gemini capturing a 8086x2880 desktop, publishing it as a file and describing both monitors](docs/screenshots/03-screen-capture.png)

Every answer was produced by `system_vitals`, `device_info`, `device_capture` and
`share_file` running on the node itself — the model only ever saw tool results.

---

## ✨ Why it works this way

| | |
| :--- | :--- |
| 📱 **Mobile-first agent** | Works in the official Gemini mobile app and on the web through Gemini Spark. |
| 🖥️ **Any PC** | Linux, macOS, Windows — desktops, laptops, VPS, containers. **Android phones and tablets** run the same node inside Termux. |
| 🌐 **Behind any NAT** | The node opens an *outbound* WebSocket tunnel. No public IP, no port forwarding, no router setup. |
| ⚡ **One-line install** | Detects OS, architecture and device type; installs autostart (`systemd` / `launchd` / Windows task / runit + Termux:Boot on Android); copies your connection link to the clipboard. |
| 🛡️ **Two credentials, no secret in a URL** | The **device token** opens the tunnel and travels only in an `Authorization: Bearer` header. The **link key** (`lk_…`) is what goes into a URL; it can run tools but cannot open a tunnel, and it is rotatable — so a leaked link is fixed in one call, not by reinstalling the node. Everything else gets HTTP 401. |
| 🔁 **Many machines, one gateway** | Each node is addressed by `?user=<node-name>` on one shared domain. |
| 🆓 **Free & open source** | MIT license, works with the free Gemini tier. |

---

## 🛠️ What Gemini can do on your machine (MCP tools)

**26 tools.** Every one of them runs on your machine under your own user account — the gateway only carries the calls.

### Which calls ask for your confirmation

Gemini Spark decides whether to stop and ask *"confirm this action?"* from the **MCP annotation hints** each tool advertises (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`). A tool advertised with no hints at all is treated as destructive by the spec's defaults — that is why a server that declares none gets a confirmation prompt on *every single call*. Here the hints are explicit:

| Class | Tools | Confirmation |
| :--- | :--- | :--- |
| Read-only | `mesh_status`, `system_info`, `system_vitals`, `get_orchestration_skill`, `list_dir`, `read_file`, `grep_search`, `glob_find`, `job_output`, `job_list`, `share_list`, `device_info` | never asked |
| Local work — commands, builds, file writes, jobs, shares, and the whole device branch | `bash_exec`, `run_job`, `write_file`, `edit_file`, `job_kill`, `share_file`, `serve_dir`, `unshare`, `device_control`, `device_capture`, `device_messages` | not asked |
| **Confirmed — install/delete, `sudo`, system paths** | `system_change` (installs/removes software, deletes data, runs `sudo`, edits system paths by shell), `system_write` (writes file content into a system path), `mesh_update` (installs a release) | **asked first** |

A prompt is spent on a **system change** only: `system_change`, `system_write` and `mesh_update` install, remove, delete or replace something you cannot undo in one call. Everything else runs silently, the device branch included — a camera, a microphone or an SMS list installs and removes nothing, and what still refuses such a call is the node's own switch (`MESH_DEVICE`, `MESH_DEVICE_ACTIONS`, `MESH_DEVICE_PIM`, `MESH_READ_ONLY`), which holds no matter what the client does. `unshare` is ordinary local work for the same reason: it stops a local server and removes a copy you can publish again in one call.

The mechanics matter if you also run the node in standalone mode: Gemini Spark skips its dialog only for a tool it sees as `readOnlyHint: true`, so the gateway advertises **every** non-system call that way — the shell and file twins because its classifier refuses the gated command classes, the device branch because the node refuses the call before any `termux-*` command runs. The node's own `tools/list` keeps the literal hint instead (a camera is not a read), which is why a client connected straight to `core/server.py` may still ask.

Four classes are gated, and the gateway — not the hint — enforces them, because a hint is static per tool and cannot tell `ls` from `apt install`:

1. **Installing or removing software** — package managers, uninstallers, `msiexec`, `dpkg`/`rpm`, `curl … | bash`, `iwr … | iex`.
2. **Deleting data** — `rm`, `Remove-Item`, `del`, `rd`, `shred`, `dd`, `mkfs`, `git clean -fd`, `docker rm`/`prune`, `truncate -s 0`.
3. **Running as root** — `sudo`, `doas`, `pkexec`, `runas`, `Start-Process -Verb RunAs`.
4. **System paths** — `/etc`, `/usr`, `/boot`, `/var/lib`, `/opt`, `/dev`, `C:\Windows`, `C:\Program Files`, `ProgramData`, the registry. Reads of those paths (`cat /etc/os-release`, `systemctl status`) are *not* gated, and neither is ordinary work in your own directories.

`bash_exec`/`run_job` refuse such commands and tell the model to re-issue them as `system_change`; `write_file`/`edit_file` refuse a system path and point at `system_write` (which takes the exact content, so there is no shell quoting to get wrong). The gate is moved, not removed — inspection, builds, tests, config edits in your projects and the like still run without a dialog.

The tool surface and the confirmation policy live in [gateway.py](gateway.py) (annotations, `INSTALL_COMMAND_PATTERNS` / `DELETE_COMMAND_PATTERNS`, `SYSTEM_PATH_RE`, `classify_command()`) and in [core/mcp_tools.py](core/mcp_tools.py) (`TOOL_ANNOTATIONS` for the node's own surface, which also carries the device branch's four tools); tests compare the two surfaces and pin the classifier, so a gated command cannot slip through.

> [!NOTE]
> The classifier matches a verb at the start of a command segment (after `sudo`/`env`/`timeout`/`powershell -Command` prefixes), so `echo "rm -rf /"` or `grep rm notes.txt` still run, while `curl … | bash` counts as an install. It is a UX gate, not a sandbox: for hard guarantees independent of any prompt use the node's own switches — `MESH_READ_ONLY=1` and `MESH_WRITE_ROOTS` ([core/agent.py](core/agent.py)).

### Status and host information

| Tool | What it does |
| :--- | :--- |
| `mesh_status()` | Confirms the node is reachable, with live evidence from the host itself: what kind of machine answered (`device_class`, `scenario`) and its `battery_percent`. Call it first if the machine looks offline. |
| `system_info()` | One-call host summary: OS, desktop, user, home, disks, memory, load, top processes, the current wallpaper, `command_shell` — the shell `bash_exec` will actually use — and the device blocks: battery and charging, network and signal, language, time and timezone, CPU, RAM, storage, cameras, microphones and sensors. |
| `system_vitals()` | CPU, RAM and disk metrics, plus the battery block and the thermal sensors the platform exposes. |

### Shell

| Tool | What it does |
| :--- | :--- |
| `bash_exec(command, timeout_sec, max_chars, cursor)` | Runs a shell command. The shell matches the **host**, not the tool's name — `bash` on Linux/macOS, PowerShell or `cmd.exe` on Windows; check `command_shell` from `system_info()` first. Output is paginated: when it is cut, call again with `cursor=next_cursor`, nothing is dropped; output above 2 MB is spooled to a file returned in `saved_to`. `timeout_sec` is 1–120 (default 25). Installs/removals, `sudo` and system paths are **refused here** and have to go through `system_change`. |
| `system_change(command, timeout_sec, background, cwd, max_chars, cursor)` | Installs, removes or deletes on the host, runs privileged commands and edits system paths — the confirmed twin of `bash_exec`, and the only tool allowed to run `apt`/`dnf`/`pacman`/`pip`/`npm`/`winget`/`msiexec`/`rm`/`Remove-Item`/`sudo` or disk tools. It is advertised as destructive, so Gemini asks you first. `background=true` runs it as a job (for long installs) and returns a `job_id` for `job_output`; `cwd` applies to that mode. |

### Files

| Tool | What it does |
| :--- | :--- |
| `list_dir(path)` | Lists files and directories (workspace by default). |
| `read_file(path, start_line, end_line, max_chars, cursor)` | Reads a text file with line numbers, optionally restricted to a line range. Paginated like `bash_exec`. |
| `write_file(path, content, create_dirs, mode)` | Atomically creates or overwrites a file (temp file + `os.replace`). `mode` is POSIX-only: on Windows it is not honoured, and the result says so instead of pretending. A path inside the system is **refused** and has to go through `system_write`. |
| `edit_file(path, old_string, new_string, expected_sha256, replace_all)` | Replaces an exact substring. `old_string` must match exactly once unless `replace_all` is set; `expected_sha256` guards against overwriting a file that changed since it was read. System paths are refused — read the file and send the whole new content to `system_write`. |
| `system_write(path, content, create_dirs, mode)` | Writes a file inside a system path (`/etc`, `/usr`, `/boot`, `/var/lib`, `C:\Windows`, `C:\Program Files`, `ProgramData`, the registry) — the confirmed twin of `write_file` and the only way to write there. Gemini asks you first. Content is exact, so there is no shell quoting to get wrong. |

### Search

| Tool | What it does |
| :--- | :--- |
| `grep_search(pattern, path, glob, limit, ignore_case, fixed, context)` | Recursively searches file contents, skipping binary files and heavy directories. `fixed` treats the pattern as literal text, `context` adds surrounding lines, `limit` is 1–1000 (default 200). |
| `glob_find(pattern, path)` | Finds files by glob pattern (`*` and `**`). |

### Long-running work

| Tool | What it does |
| :--- | :--- |
| `run_job(command, cwd)` | Starts a command in the background and returns a `job_id`. On an Android node it takes the wake lock for the lifetime of the job, so Android does not freeze it when the screen goes off. |
| `job_output(job_id, wait_ms, max_chars, cursor)` | Reads a job's output, optionally waiting up to 20 s for completion. Paginated. |
| `job_kill(job_id, signal)` | Terminates a job. `signal` is `TERM` (default), `KILL`, `INT`, `HUP` or `QUIT`. |
| `job_list(limit)` | Lists recent jobs, newest first (20 by default, max 50), including whether each one holds a `wake_lock`. |

### Device (phone, laptop or server)

These four tools read and act on the device the node itself runs on. They are advertised on **every** node — a phone-only action on a laptop answers with `available: false`, the reason and, where one exists, the fix, instead of failing.

| Tool | What it does |
| :--- | :--- |
| `device_info(section, sensor, fresh, quick)` | One-call device report: battery and charging, network interfaces with Wi-Fi and cellular signal, language, time and timezone, CPU, RAM, storage, cameras, microphones, sensors and what the platform can actually reach. `section` is `summary`, `all` (default) or one of `device`, `battery`, `network`, `locale`, `time`, `hardware`, `storage`, `cameras`, `microphones`, `sensors`, `capabilities`. `sensor` takes one live sample from a named sensor (Android; the name comes from the `sensors` block), `fresh` bypasses the few-second cache and `quick` skips the slow sections. Every block carries `available` and `source`; a block the platform cannot answer says why instead of showing a zero. |
| `device_control(action, on, value, stream, text, title, id, url, path, state, timeout_sec)` | Twenty reversible actions: `torch`, `vibrate`, `volume`, `volume_get`, `brightness`, `tts_speak`, `toast`, `notify`, `notify_list`, `notify_remove`, `clipboard_get`, `clipboard_set`, `media`, `media_scan`, `wakelock`, `download`, `open`, `share`, `dialog`, `wallpaper`. `value` is milliseconds for `vibrate`, a 0–15 level for `volume` and 0–255 for `brightness`; `stream` picks the audio stream (`music` by default); `text` is the body for TTS, toast, notification, clipboard and dialog, and `play\|pause\|stop\|info` for `media`. Nothing here is destructive. |
| `device_capture(action, camera_id, path, seconds, provider, frequency, pattern)` | Eleven actions: `camera_list`, `camera_photo`, `mic_record_start`, `mic_record_stop`, `mic_record_status`, `location`, `fingerprint`, `usb_list`, `usb_access`, `infrared_frequencies`, `infrared_transmit`. **No confirmation prompt** — it installs and removes nothing, and `MESH_DEVICE`/`MESH_DEVICE_ACTIONS`/`MESH_READ_ONLY` on the node are what refuse a call. `path` is the output file for `camera_photo` and `mic_record_start` (default: the node's capture directory) and the device path from `usb_list` for `usb_access`; `camera_id` comes from `camera_list`, `seconds` limits a recording, `provider` is `gps`, `network` or `passive`, and `frequency`/`pattern` drive `infrared_transmit`. A photo or a recording stays in the capture directory and is never uploaded by itself — `share_file` publishes it only when you ask for that. `fingerprint` returns only the verdict; `usb_access` asks Android for permission and returns the descriptor but deliberately refuses to run a program for you. |
| `device_messages(action, number, text, limit, offset, type, query)` | Five actions: `sms_list`, `sms_send`, `call_log`, `contacts`, `call`. **No confirmation prompt, and off until the operator enables it with `MESH_DEVICE_PIM=1`** — it is the private data of whoever holds the phone, so the node itself is the gate. `limit` is 1–50 (default 10), `offset` pages through the list, `type` filters it and `query` filters contacts on the node. |

Example:

```text
"Check the phone's battery and signal."
  → device_info(section="summary")

"Turn on the torch."
  → device_control(action="torch", on=true)

"Take a photo and share it with me."
  → device_capture(action="camera_photo")   # runs without a dialog
  → share_file(path=<the path it returned>)
```

### Device switches (operator)

Read from `agent.env` at startup ([core/agent.py](core/agent.py), applied in [core/mcp_tools.py](core/mcp_tools.py) and [core/device.py](core/device.py)):

| Variable | Default | What it does |
| :--- | :--- | :--- |
| `MESH_DEVICE` | `auto` | `auto` keeps the branch answering everywhere (only the answer differs per platform), `0` switches the branch off, `1` forces it on. |
| `MESH_DEVICE_ACTIONS` | empty | Comma-separated allowlist of action names for `device_control`, `device_capture` and `device_messages`; empty means every action the build implements. |
| `MESH_DEVICE_CAPTURE_DIR` | shared storage on a phone once `termux-setup-storage` has been granted (`~/storage/dcim/remote-harness-mcp` or `~/storage/shared/RemoteHarnessMCP`), otherwise `~/.cache/remote-harness-mcp/captures` | Where photos and recordings are written. |
| `MESH_DEVICE_PIM` | `0` | `1` enables SMS, the call log, contacts and placing a call. |
| `MESH_DEVICE_QUICK` | `0` | `1` keeps `system_info` instant by skipping network, cameras, microphones and sensors. |
| `MESH_READ_ONLY` | `0` | `1` refuses every `device_control` and `device_capture` action, and blocks `sms_send` and `call`. This is the node-side guarantee that does not depend on any client prompt. |

### Publishing files and web pages

These four make something on your machine readable from the internet. The link is served by the gateway on the shared domain, so **the node has to stay connected for it to work**.

| Tool | What it does |
| :--- | :--- |
| `share_file(path, name, overwrite)` | Publishes **one file**: it is copied into the node's share root and you get `https://<shared-domain>/<node>/<name>-<random>/<filename>`. `overwrite` replaces an existing share with the same name. |
| `serve_dir(path, name)` | Serves a **directory** in place — no copy is made. `index.html` is used when present, otherwise a directory listing is shown. Read-only (GET/HEAD only). Returns `https://<shared-domain>/<node>/<name>-<random>/`. |
| `share_list()` | Lists active shares with their URLs, their roots, and whether the local server is running. |
| `unshare(name)` | Stops a share and revokes its link. Accepts the share name, its slug or the full URL. A file share also deletes the copy in the share root; a served directory is left untouched on disk. |

> [!IMPORTANT]
> The random part of the path **is** the credential — anyone who has the link can read the file or browse the directory. Treat a share link like a password, and `unshare` it when you are done. Size limits: 32 MiB per file by default, 64 MiB ceiling via `MESH_WEB_MAX_BYTES`.

### Agent behaviour

| Tool | What it does |
| :--- | :--- |
| `get_orchestration_skill()` | Loads the agent's current operating rules and orchestration skill. |

### Keeping the node up to date

| Tool | What it does |
| :--- | :--- |
| `mesh_update(action, force, offline, restart)` | Reports, checks for or installs a newer release of the node's own code. `status` reads local state (no network), `check` asks GitHub, `apply` downloads the payload, verifies its published SHA-256, installs it with a rollback backup and restarts the agent. |

> [!TIP]
> Nodes update themselves: a running agent checks for a newer release in the background (every 6 hours by default) and installs what it finds, and on Windows the `RemoteHarnessMCPUpdater` task checks once a day even when no agent is running. `MESH_UPDATE_AUTO=0` switches that to "report only". Details, environment variables and rollback: [docs/UPDATES.md](docs/UPDATES.md).

> [!TIP]
> Gemini gives a single tool call roughly 30 seconds. For anything longer (builds, backups, downloads) the agent uses `run_job` and polls `job_output`, so tasks never get cut off.

---

## 📦 Historical upstream quick start

> For our Gateway and Google login, follow [the current guide](#-подключите-своё-устройство-к-chatgpt) above. The commands below target the upstream deployment.

### 1. Install the node on the PC you want to control

**🐧 Linux / 🍎 macOS**
```bash
curl -fsSL https://racknerd-5a24bf9.merino-carob.ts.net/install.sh | bash
```

**📱 Android (Termux)** — no root, works from F-Droid's Termux
```bash
curl -fsSL https://racknerd-5a24bf9.merino-carob.ts.net/install.sh | bash
```
Autostart on a phone is a runit service plus the Termux:Boot app, and the node is
named after the device model — see [docs/TERMUX.md](docs/TERMUX.md).

**🪟 Windows (PowerShell)**
```powershell
irm https://racknerd-5a24bf9.merino-carob.ts.net/install.ps1 | iex
```

**🪟 Windows — visual installer** (from a clone or a release)

```powershell
.\install-gui.cmd
```

A WinForms wizard that mirrors the console installer's variants, switches language at
runtime and shows the MCP link at the end — see [docs/GUI_INSTALLER.md](docs/GUI_INSTALLER.md).

**📦 Node.js (any OS)**
```bash
npx remote-harness-mcp
```

The pre-rename name still works: `npx gemini-computer-use` is an alias package
that depends on `remote-harness-mcp` and forwards every argument to the same CLI
(see [npm/gemini-computer-use](npm/gemini-computer-use/README.md)).

**🛠️ From source**
```bash
git clone https://github.com/LevRa7/remote-harness-mcp.git
cd remote-harness-mcp
./install.sh --quick
```

At the end the installer prints your personal MCP link and copies it to the clipboard:
```text
https://racknerd-5a24bf9.merino-carob.ts.net/sse?user=<your-node-name>&token=<your_secret_token>
```

### 2. Connect it to Gemini Spark

1. Open **[gemini.google.com/customize/connectors](https://gemini.google.com/customize/connectors)** in a browser or in the Gemini mobile app.
2. Click **Add connector** (or **Settings ⚙️ → Connectors**) and name it **MyDevice**.
3. Paste the link (**Ctrl+V**) into the **Server URL** field and click **Connect**.

### 3. Give it a task

> *"Show system_vitals and list the 5 biggest folders in my home directory."*

That's it — Gemini is now an agent running on your machine. Repeat step 1 on other computers to control all of them from the same phone.

---

## 🖥️ Deployment modes

| Mode | Command | When to use |
| :--- | :--- | :--- |
| **Gateway + tunnel** (recommended) | `./install.sh --quick` | Home PCs and laptops behind NAT — the mobile-agent scenario. |
| **Remote over SSH** | `./install.sh --ssh=user@host` | Deploy a node onto a remote Linux server from your terminal. |
| **Local standalone** | `./install.sh --mode=standalone --port=8096` | Pure localhost FastMCP server at `http://localhost:8096/sse`, no cloud relay. |

On Windows the same variants are available in the visual installer (`.\install-gui.cmd`); local standalone stays console-only there (`.\install.ps1 -Mode standalone -Port 8096`).

On **Android/Termux** the same `./install.sh` variants work; autostart is a runit service plus the Termux:Boot app instead of systemd, and standalone stays reachable from that phone alone — see [docs/TERMUX.md](docs/TERMUX.md).

---

## 🪟 Windows installation

Windows already ships PowerShell 5.1, so there is nothing to prepare — no Python, no Node.js, no administrator rights. Both installers finish by putting the same MCP link on the clipboard.

|  | Console installer | Visual installer |
| :--- | :--- | :--- |
| Start | `irm https://racknerd-5a24bf9.merino-carob.ts.net/install.ps1 \| iex` | `.\install-gui.cmd` |
| Needs | nothing but PowerShell | a clone or an unpacked release — it drives `install.ps1`, `core/` and `install.sh` |
| Variants | `-Mode tunnel` (default), `-Mode standalone`, `-User`, `-Gateway`, `-Token`, `-Port`, `-DryRun` | Quick setup, Custom setup, Remote over SSH |
| Language | `-Lang en` / `-Lang ru` | switch in the window header, or `-Lang` |

### Automatic dependencies and architecture

Nothing has to be prepared by hand first. Both installers obtain what they need, and if
they cannot, they stop with an explicit message instead of writing an autostart entry that
can never work.

| | Windows (`install.ps1`) | Linux / macOS (`install.sh`) |
| :--- | :--- | :--- |
| Python | `winget` → the python.org installer for **this** architecture → `uv` | `apt` / `dnf` / `yum` / `zypper` / `pacman` / `apk` / `xbps` / `brew` → `uv` |
| `websockets` | `pip` → `pip --user` → `ensurepip` → `uv` → a venv | `pip` → `pip --break-system-packages` → `pip --user` → `ensurepip` → `uv` → a venv |
| If all of that fails | stops, and prints the exact command to run | stops, and prints the exact command to run |

`websockets` is the only external Python requirement: `core/mcp_tools.py` and
`core/server.py` are standard library only. `uv` is fetched only when the cheaper paths
have already failed.

**The architecture decides which build is downloaded.** The installer asks the *OS*, not
the process, because a 32-bit PowerShell on 64-bit Windows reports `x86` and hides the
real value in `PROCESSOR_ARCHITEW6432`, and an emulated x64 process on ARM64 reports
`AMD64`:

| Machine | Python build | `uv` archive |
| :--- | :--- | :--- |
| Windows x64 | `python-3.12.5-amd64.exe` | `x86_64-pc-windows-msvc` |
| Windows ARM64 | `python-3.12.5-arm64.exe` | `aarch64-pc-windows-msvc` |
| Windows 32-bit | `python-3.12.5.exe` | — (uv ships no 32-bit Windows build) |
| Linux `x86_64` | the package manager's own build | `x86_64-unknown-linux-gnu` |
| Linux `aarch64` | the package manager's own build | `aarch64-unknown-linux-gnu` |
| Linux `armv7l` / `i686` | the package manager's own build | `armv7-unknown-linux-gnueabihf` / `i686-unknown-linux-gnu` |
| macOS `arm64` / `x86_64` | `brew`, or `uv` | `aarch64-apple-darwin` / `x86_64-apple-darwin` |

The interpreter that ends up pinned is the one that actually has `websockets` — which can
be a venv or a `uv`-managed CPython, both deliberately outside `PATH`. `install.ps1 -DryRun`
and `install.sh --dry-run` report the architecture without changing anything, and the visual
installer shows it in its preflight.

### Download the setup executable

The release also ships a single compiled installer, for machines where you would rather
not clone or download anything else — `RemoteHarnessMCP-Setup-<version>.exe`:

```powershell
.\RemoteHarnessMCP-Setup-0.4.8.exe            # open the visual installer
.\RemoteHarnessMCP-Setup-0.4.8.exe -Lang ru   # start in Russian
.\RemoteHarnessMCP-Setup-0.4.8.exe -SelfTest  # headless self-check, prints JSON
.\RemoteHarnessMCP-Setup-0.4.8.exe --version  # print the version
```

It carries the wizard, `install.ps1`, `core/` and `install.sh` inside itself, unpacks them
to `%LOCALAPPDATA%\RemoteHarnessMCP\setup\<version>` (override with `MESH_SETUP_DIR`) and
runs the wizard from there. It needs nothing but the .NET Framework and PowerShell that
Windows already has, and it is built from this repository by
`.\build-installer-exe.ps1` — no SDK, no NuGet, no network.

It is **not code-signed**, so SmartScreen may ask for confirmation the first time you run
it.

### Visual installer

![Windows visual installer](docs/images/gui-welcome-en.png)

- **Quick setup** (recommended) — node name = PC name, shared gateway domain, Windows autostart. The one to pick on a home PC or laptop.
- **Custom setup** — your own node name, your own shared domain, optional token.
- **Remote over SSH** — copy the node code to a Linux host and run `install.sh` there. Needs the Windows *OpenSSH Client* feature and key-based authentication; interactive password prompts are not supported.

The first screen runs `install.ps1 -DryRun` and reports what it found — Python, `websockets`, the resolved gateway domain, the config directory, the autostart path — without changing anything. Before starting, the window shows the exact command it will run; during the run it streams the installer output; at the end it shows the MCP link with a copy button, the three-step Gemini instructions and the paths it created.

Full details, screenshots and the SSH notes: [docs/GUI_INSTALLER.md](docs/GUI_INSTALLER.md).

### What a Windows installation creates

| Path | What it is |
| :--- | :--- |
| `%USERPROFILE%\.config\remote-harness-mcp\agent.env` | `MESH_GATEWAY`, `MESH_USER`, `MESH_TOKEN` |
| `%USERPROFILE%\.config\remote-harness-mcp\domain.env` | `MESH_PUBLIC_URL` — the resolved shared domain, so the node's share links, its tunnel and the gateway name the same host (not written when the built-in default is all that is configured) |
| `…\Start Menu\Programs\Startup\rhm-agent.vbs` | autostart entry, so the node comes back after a reboot |
| `%USERPROFILE%\.config\remote-harness-mcp\agent.log` | agent output, for when a silent autostart fails |

### Language

The visual installer starts in the language of your Windows language settings — the display language and the preferred-languages list are read from the registry — and falls back to English. `-Lang ru` / `-Lang en` overrides the detection. The switch in the window header changes every label immediately.

### Troubleshooting

- **"No gateway domain configured"** — this copy was not published with a domain. Pass one: use *Custom setup* in the wizard, or `.\install.ps1 -Gateway <shared-domain>`, or set `MESH_PUBLIC_URL`.
- **Registration fails** — the installer stops *before* writing anything when the gateway cannot be reached. Check that the domain resolves, then retry with an explicit `-Gateway`.
- **`python` opens the Microsoft Store** — that is the App Execution Alias, not an interpreter. Both installers resolve a real `python.exe` by full path and ignore the alias; the wizard never pins an alias into the autostart entry.
- **The node does not come back after a reboot** — run `.\ops\doctor.ps1`: it prints the autostart interpreter, the heartbeat age, the last `agent.log` lines (including a torn final line) and the gateway's own view of this node. `.\ops\windows\agent-watchdog.ps1` performs that check once and restarts the agent; the scheduled task `RemoteHarnessMCPWatchdog` runs it every five minutes, so a dead agent comes back on its own.

### Standalone on Windows

The wizard does not offer it; use the console installer:

```powershell
.\install.ps1 -Mode standalone -Port 8096
```

That starts a local-only FastMCP server at `http://localhost:8096/sse` and puts that URL on the clipboard.

---

## 📱 Android installation (Termux)

The same `install.sh` installs a node on an unrooted phone or tablet, and the phone
then shows up in Gemini Spark like any other machine. Full guide:
**[docs/TERMUX.md](docs/TERMUX.md)**.

```bash
# 1. Termux from F-Droid (not Google Play); optionally Termux:Boot and Termux:API
pkg update -y
# 2. the usual one-liner
curl -fsSL https://racknerd-5a24bf9.merino-carob.ts.net/install.sh | bash
```

What a phone changes, and how the installer handles it:

| | |
| :--- | :--- |
| **Packages** | `pkg` instead of `apt`, `python`/`python-pip` instead of `python3-pip`/`python3-venv` — a phone is never root and has no `sudo`. |
| **Node name** | The device model (`Pixel 7 Pro` → `pixel7pro`): Android answers `localhost` to every app, and the gateway keeps one tunnel per name. |
| **Domain file** | `~/.config/remote-harness-mcp/domain.env` — there is no `/etc` to write to. The installer records the resolved shared domain there, so the phone's share links, its tunnel and the gateway name the same host (the built-in default is never recorded). |
| **Autostart** | A **runit** service (`termux-services`, the `Restart=always` equivalent) plus a **Termux:Boot** script that takes the wake lock after a reboot. |
| **Clipboard** | `termux-clipboard-set` (Termux:API), so the MCP link goes straight into the Gemini app. |

> [!IMPORTANT]
> Two Android-side switches are yours to flip: install **Termux:Boot** and open it
> once, and set battery optimisation to *Unrestricted* for Termux. Without them
> Android unloads the node with the screen off.

The phone must also be able to **resolve the gateway name**. A gateway that lives on
a private network (Tailscale, a VPN, a DNS override on your laptop) resolves there and
nowhere else — mobile data resolves nothing private. Put the phone on that network, or
give the gateway a publicly resolvable domain; the installer detects a name it cannot
resolve and says so before writing anything: see
[docs/TERMUX.md](docs/TERMUX.md#private-gateway-tailscale--vpn).

```bash
sv status rhm-agent                       # is it up?
sv restart rhm-agent                      # restart now
tail -f $PREFIX/var/log/sv/rhm-agent/current
```

`--mode=standalone` also works, but it serves `127.0.0.1` only — reachable from
inside that phone alone. Use the default tunnel mode to drive the phone from Gemini.

---

## 🔄 Automatic updates

A node installs itself from a GitHub release and then keeps itself current the same
way. You do not re-install anything by hand, and nothing unverified is installed.

- **It checks by itself.** A running agent asks the GitHub Releases API every 6
  hours (`MESH_UPDATE_CHECK_INTERVAL`), compares the tag with the version in
  `core/version.py` and installs what it finds. On Windows the daily
  `RemoteHarnessMCPUpdater` task does the same for a machine whose agent is not
  running. `MESH_UPDATE_AUTO=0` turns installing off and keeps reporting.
- **It verifies before it installs.** The payload is downloaded, its SHA-256 is
  compared with the published checksum, and only then are `core/`, `skills/` and
  `ops/` replaced. No checksum, no update.
- **It can always go back.** The replaced directories are saved under
  `%USERPROFILE%\.config\remote-harness-mcp\backups\<version>-<timestamp>\`, and an
  update interrupted by a kill or a power cut is rolled back automatically on the
  next start.
- **The node comes back new.** The agent is restarted through whatever supervises
  it (the watchdog task on Windows, `systemctl --user restart rhm-agent.service`,
  `launchctl kickstart -k`) — or started directly when nothing supervises it.

By hand, any time:

```powershell
remote-harness-mcp update -Check      # is a newer release published?
remote-harness-mcp update             # install it now and restart
remote-harness-mcp update -Check -Json
```

From Gemini: ask the node to run `mesh_update` (`status`, then `check`, then
`apply`).

The state of the last check is in the heartbeat, so `ops/doctor.ps1` shows it, and
the full history is in `%USERPROFILE%\.config\remote-harness-mcp\update.log`.
Configuration, exit codes, rollback and troubleshooting: [docs/UPDATES.md](docs/UPDATES.md).

---

## 🔗 One shared domain (canonical URL contract)

A node is **never** addressed by its own hostname. The gateway publishes a single shared domain, and the node name travels as a query parameter:

| Purpose | Canonical URL |
| :--- | :--- |
| MCP over SSE | `https://<shared-domain>/sse?user=<node-name>&token=<link-key>` |
| MCP Streamable HTTP | `https://<shared-domain>/mcp?user=<node-name>&token=<link-key>` |
| Reverse tunnel (agent) | `wss://<shared-domain>/ws/tunnel?user=<node-name>` + `Authorization: Bearer <device-token>` |

Why: every extra hostname would need its own DNS record *and* its own SAN in the TLS certificate. A node whose name is missing from the certificate fails the TLS handshake, and the Gemini client then reports an opaque "cannot connect to host". With one shared domain the certificate covers every node forever, and adding a node is only a registration call.

- The shared domain defaults to `smart-server.online`; override it with `./install.sh --domain=<shared-domain>` (node side) or `MESH_PUBLIC_URL` (gateway side).
- Legacy per-device subdomain URLs still resolve for backwards compatibility and log a deprecation warning; set `MESH_LEGACY_SUBDOMAIN=0` on the gateway to reject them outright.
- The installer now records the domain it resolved in the domain file, so the node's share links, its tunnel and the gateway all name the same host: `MESH_PUBLIC_URL=https://<shared-domain>` in `domain.env`, written in both install branches (standalone, and cloud-gateway/tunnel right after `agent.env`). The value is normalised first, the `__MESH_DOMAIN__` placeholder and an empty value are never written, the write is skipped when the file already names that host, and a file that cannot be written is not fatal — the installer prints the exact command to run by hand. The built-in default is never recorded at all: it is a fallback, not a configuration, and pinning it would stop `core/domain.py` from consulting the legacy `MESH_GATEWAY` at all, so a later change in `agent.env` would be silently ignored — nothing is lost, because with no file that same value is already the resolver's last fallback. `--dry-run` reports the file and the value it would write — including `not written (the built-in default is a fallback, not a configuration)` — and still changes nothing on disk.
- Why this matters: share links are built by `core/domain.py`, whose chain is `MESH_PUBLIC_URL` → `AGY_PUBLIC_BASE_URL` → the domain file → the built-in default, and which deliberately never reads the legacy `MESH_GATEWAY` that `agent.env` carries (only `gateway_host()`, the tunnel host, does). A node installed the normal way — `agent.env` with `MESH_GATEWAY`/`MESH_USER`/`MESH_TOKEN` and no domain file — therefore dialled the right gateway while minting links on the built-in default. On an already-installed node, re-run the installer or write the one line by hand: `mkdir -p ~/.config/remote-harness-mcp && echo 'MESH_PUBLIC_URL=https://<domain>' > ~/.config/remote-harness-mcp/domain.env`, or the same with `sudo tee /etc/remote-harness-mcp/domain.env` on Linux. Existing share links keep working; new ones use the configured domain.
- `MESH_DOMAIN_FILE` only overrides the *path*, and it belongs to the node's own configuration rather than to the installer invocation: keep it in `agent.env` (every `MESH_*` key there is exported to the node), or the domain is written into a file the node never reads. The installer creates the parent directory of whichever file is in effect. Over `--ssh=<host>` the domain resolved here is handed to the remote `install.sh` as `--domain=`, so the target records that host instead of re-resolving it (a repository copy still carrying the `__MESH_DOMAIN__` placeholder would otherwise fall back to the built-in default).

---

## 🔒 Security

- **Two credentials per node** — the *device token* opens the tunnel and is accepted only in an `Authorization: Bearer` header; the *link key* (`lk_…`) is the only credential allowed in a URL. The device token in a query string is rejected with HTTP 401.
- **Link keys rotate** — `POST /v1/devices/<node>/rotate` issues a new link key and kills the old URL; `POST /v1/devices/<node>/revoke` removes the node. Neither needs a reinstall.
- **The device token rotates too** — no reinstall. From the node's payload directory run `python3 -m core.rekey` (Windows: `cd $env:USERPROFILE\.remote-harness-mcp; python -m core.rekey`). It asks the gateway for a new token, writes it into `agent.env`, the launchd plist on macOS and the Startup launcher on Windows, restarts the supervisor (systemd / launchd / Scheduled Task / runit) and waits for the node to report online. The previous token keeps working until the node connects with the new one, so a failed restart restores the old token instead of locking the machine out. `POST /v1/devices/<node>/rotate-token/finish` retires it immediately when the old token is known to be in someone else's hands. The `remote-harness-mcp rotate` shorthand is the same command through the npm CLI.
- **Enrolment** — `/api/register` refuses to hand a token to a request that only knows a name or a MAC address; possession of the device token is required to reuse a registration. Set `MESH_REGISTER_MODE=pair` (with `MESH_ADMIN_TOKEN`) to replace self-registration entirely with the pairing flow: `POST /v1/devices/pair` → approve the code → collect the credentials.
- **Outbound-only tunnel** — nodes never listen on public ports; the agent dials out to the gateway.
- **Gateway is a relay** — it terminates TLS and forwards calls; commands run only on your node.
- **Unprivileged by default** — the agent runs in user space, without root.

> [!WARNING]
> Whoever has your MCP link can run commands on that machine. Treat it like a password: don't publish it, and rotate the link key (`POST /v1/devices/<node>/rotate`) if it leaks.

---

## 🚢 Self-hosting the gateway

The gateway is a single host serving every node. Publish it only through the deploy script, so the running gateway, the served installers and the node bootstrap payload cannot drift apart:

```bash
MESH_GATEWAY_SSH=root@<gateway-ip> ./deploy_gateway.sh              # deploy
MESH_GATEWAY_SSH=root@<gateway-ip> ./deploy_gateway.sh --dry-run    # preview
MESH_GATEWAY_SSH=root@<gateway-ip> MESH_GATEWAY_SSH_PASS_FILE=~/.ssh/gw-pass \
    ./deploy_gateway.sh                                            # password auth
```
It verifies every upload with an sha256 manifest on the host, takes a timestamped backup, installs with the right owner, restarts the service and finishes with a public health check. Nothing is installed if verification fails.

> [!TIP]
> **One place for the domain.** `MESH_PUBLIC_URL` in `domain.env`
> (`/etc/remote-harness-mcp/domain.env` on Linux, `%USERPROFILE%\.config\remote-harness-mcp\domain.env`
> on Windows) is the only setting that names it: the node, the gateway, the installers, the public
> file links and the nginx vhosts all resolve it through `core/domain.py`. Switching domains is one
> value plus `ops/nginx/render-domain.sh --apply` — see [docs/DOMAIN.md](docs/DOMAIN.md).

> [!IMPORTANT]
> **`install.ps1` encoding — one file, two representations.** The repository copy is UTF-8 *with* a BOM: Windows PowerShell 5.1 decodes a BOM-less script with the ANSI code page, the Russian strings then turn into smart quotes and the parser rejects the whole file, so `.\install.ps1` from a clone would not run. The copy the gateway serves is written *without* the BOM by `deploy_gateway.sh`, because `irm … | iex` receives the BOM as part of the first token and `param(...)` then stops being the first statement. nginx declares `charset utf-8` for that location, so the BOM-less copy still decodes correctly. Please keep both properties when editing the file.

**TLS certificate policy — one name, no per-device SANs.** The certificate must cover the shared domain only; a node is selected by `?user=`, never by a hostname:

```bash
# what the certificate currently covers
sudo certbot certificates | grep -A1 'Certificate Name: smart-server.online'

# canonical renewal check (staging, does not touch the live certificate)
sudo certbot renew --dry-run --cert-name smart-server.online
```
Legacy per-device SANs may still be present from before this contract. They are harmless and keep old bookmarked URLs working; drop them at the next renewal once no client uses subdomain URLs any more (see the `domains =` line in `/etc/letsencrypt/renewal/<name>.conf`, or re-issue with a single `-d`).

---

## 📄 License

**MIT** — see [LICENSE](LICENSE).
