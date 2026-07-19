# CapCut Vision MCP

A visible, session-based Windows desktop control bridge that lets an MCP client operate CapCut through screenshots, mouse/keyboard input, window controls, and user-approved asset folders.

## What it is

- Full desktop vision while a session is connected.
- Mouse, keyboard, drag, scroll, window focus, and hotkey control.
- CapCut helpers for opening the app, importing a local asset, and opening project/export folders.
- File search and browsing inside folders approved by the PC owner.
- A persistent on-screen **AI CONNECTED** banner.
- Immediate pause/kill controls.
- Every command is logged locally.

## What it is not

- No hidden service.
- No automatic startup.
- No arbitrary shell or PowerShell execution.
- No browser-cookie, credential-store, password-manager, or token extraction.
- No access while the local user has not explicitly connected the session.

## Architecture

```text
ChatGPT MCP client
        |
        v
Public relay + MCP server
        |
        v
Windows agent on the user's PC
        |
        +--> screenshots / window tree / results
        +<-- approved UI and asset commands
```

The first release uses authenticated HTTPS long-polling so it can run behind ordinary cloud hosting without exposing an inbound port on the PC.

## Agent capabilities

- `screenshot`
- `list_windows`
- `focus_window`
- `click`, `double_click`, `right_click`
- `move`, `drag`, `scroll`
- `type_text`, `press_key`, `hotkey`
- `open_capcut`, `open_explorer`, `open_path`
- `list_directory`, `search_files`, `file_info`
- `import_into_capcut`
- `wait`

## Security model

1. The local user launches the agent manually.
2. The agent displays a pairing code and a visible connection banner.
3. The MCP server can send commands only while that session is active.
4. File APIs are restricted to the configured allowed roots.
5. `Ctrl+Shift+F12` immediately pauses all remote input.
6. Closing the banner exits the agent and invalidates the session.

## Windows setup

1. Install Python 3.12 or newer.
2. In `agent/`, run:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy config.example.json config.json
python agent.py
```

3. Enter the relay URL and pairing token in `config.json`.
4. Press **Connect** in the visible agent window.

## Current status

The repository branch contains the Windows control agent and protocol contract first. The relay/MCP deployment is kept separate so it can be tested without risking the desktop agent.
