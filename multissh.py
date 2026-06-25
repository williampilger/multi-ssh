#!/usr/bin/env python3
"""
multi-ssh — Execute comandos SSH em múltiplos hosts simultaneamente.

Uso:
  multissh                    # menu interativo
  multissh run "uptime"       # roda em hosts selecionados interativamente
  multissh run --tag web df   # roda nos hosts com tag 'web'
  multissh run --all "df -h"  # roda em todos os hosts
  multissh script deploy.sh   # envia e executa script
  multissh add                # cadastra novo host
  multissh connect [NOME]     # abre sessão SSH interativa em um host
  multissh explore [NOME]    # explorador de arquivos SFTP em um host
"""

import json
import os
import sys
import threading
import argparse
import shlex
import subprocess
import shutil
import stat as _stat_mod
from pathlib import Path
from datetime import datetime as _dt
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

# ── Verificação de dependências ───────────────────────────────────────────────

_missing = []
try:
    import paramiko
except ImportError:
    _missing.append("paramiko")

try:
    import questionary
    from questionary import Choice, Separator, Style as QStyle
except ImportError:
    _missing.append("questionary")

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
    from rich.markup import escape
    from rich.rule import Rule
    from rich.text import Text
except ImportError:
    _missing.append("rich")

if _missing:
    print(f"[multi-ssh] Dependências ausentes: {', '.join(_missing)}")
    print(f"[multi-ssh] Instale com:  pip install {' '.join(_missing)}")
    sys.exit(1)

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style as PTStyle

# ── Configuração ──────────────────────────────────────────────────────────────

CONFIG_DIR      = Path.home() / ".config" / "multi-ssh"
HOSTS_FILE      = CONFIG_DIR / "hosts.json"
DEFAULT_TIMEOUT = 5   # segundos para conexão SSH

console = Console()

_style = QStyle([
    ("qmark",        "fg:#5f87ff bold"),
    ("question",     "bold"),
    ("answer",       "fg:#5f87ff bold"),
    ("pointer",      "fg:#5f87ff bold"),
    ("highlighted",  "fg:#5f87ff bold"),
    ("selected",     "fg:#00af5f"),
    ("separator",    "fg:#6c6c6c"),
    ("instruction",  "fg:#858585 italic"),
    ("text",         ""),
    ("disabled",     "fg:#858585 italic"),
])

# ── Armazenamento de hosts ────────────────────────────────────────────────────

def load_hosts() -> dict:
    if not HOSTS_FILE.exists():
        return {}
    try:
        with open(HOSTS_FILE, encoding="utf-8") as f:
            return json.load(f).get("hosts", {})
    except (json.JSONDecodeError, IOError):
        return {}


def save_hosts(hosts: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(HOSTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"hosts": hosts}, f, indent=2, ensure_ascii=False)
    # Restringe permissões em Unix (senhas ficam neste arquivo)
    if os.name != "nt":
        os.chmod(HOSTS_FILE, 0o600)


def all_tags(hosts: dict) -> list:
    tags = set()
    for h in hosts.values():
        tags.update(h.get("tags", []))
    return sorted(tags)


def hosts_by_tags(hosts: dict, tags: list) -> list:
    return [n for n, h in hosts.items() if any(t in tags for t in h.get("tags", []))]


def hosts_by_tags_advanced(hosts: dict, or_tags: list, and_tags: list) -> list:
    """
    or_tags:  host precisa ter PELO MENOS UMA dessas tags (se lista não vazia)
    and_tags: host precisa ter TODAS essas tags
    """
    result = []
    for n, h in hosts.items():
        htags = set(h.get("tags", []))
        if and_tags and not all(t in htags for t in and_tags):
            continue
        if or_tags and not any(t in htags for t in or_tags):
            continue
        result.append(n)
    return result

# ── SSH ───────────────────────────────────────────────────────────────────────

def _connect(host_info: dict, timeout: int) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(
        hostname=host_info["host"],
        port=host_info.get("port", 22),
        username=host_info["user"],
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
    )
    if host_info.get("password"):
        kwargs["password"] = host_info["password"]
        kwargs["look_for_keys"] = False
        kwargs["allow_agent"] = False
    else:
        kwargs["look_for_keys"] = True
        kwargs["allow_agent"] = True
    client.connect(**kwargs)
    return client


def _wrap_sudo(client: paramiko.SSHClient, command: str) -> str:
    """Retorna o comando prefixado com sudo -S se o host for Unix; caso contrário retorna inalterado."""
    try:
        _, uname_out, _ = client.exec_command("uname -s", timeout=5)
        if uname_out.read().decode("utf-8", errors="replace").strip():
            return f"sudo -S -p '' {command}"
    except Exception:
        pass
    return command


def _sudo_stdin(stdin, info: dict):
    """Escreve a senha no stdin para o sudo -S, se disponível."""
    password = info.get("password")
    if password:
        stdin.write(password + "\n")
        stdin.flush()


def ssh_run(name: str, info: dict, command: str, timeout: int = DEFAULT_TIMEOUT, pty: bool = False, use_sudo: bool = False) -> tuple:
    """Retorna (name, exit_code, stdout, stderr, elapsed_ms)."""
    t0 = time.monotonic()
    client = None
    try:
        client = _connect(info, timeout)
        effective_cmd = _wrap_sudo(client, command) if use_sudo else command
        stdin, stdout, stderr = client.exec_command(effective_cmd, timeout=timeout * 6, get_pty=pty)
        if use_sudo and effective_cmd != command:
            _sudo_stdin(stdin, info)
        code = stdout.channel.recv_exit_status()
        out  = stdout.read().decode("utf-8", errors="replace")
        # Com PTY, stderr é mesclado no stdout pelo terminal
        err  = "" if pty else stderr.read().decode("utf-8", errors="replace")
    except Exception as e:
        code, out, err = -1, "", str(e)
    finally:
        if client:
            client.close()
    return name, code, out, err, int((time.monotonic() - t0) * 1000)


def ssh_script(name: str, info: dict, script_path: str, timeout: int = DEFAULT_TIMEOUT, pty: bool = False, use_sudo: bool = False) -> tuple:
    """Envia e executa um script. Detecta SO remoto: bash no Unix, PowerShell/cmd no Windows."""
    t0 = time.monotonic()
    client = None
    try:
        client = _connect(info, timeout)
        os_type = _detect_os_type(client)
        script_name = Path(script_path).name
        ext = Path(script_path).suffix.lower()
        sftp = client.open_sftp()

        if os_type == "windows":
            try:
                sftp_home = sftp.normalize(".")
            except Exception:
                sftp_home = "/C:/Windows/Temp"
            remote_sftp = f"{sftp_home.rstrip('/')}/_mssh_{script_name}"
            sftp.put(script_path, remote_sftp)
            sftp.close()
            # Converte /C:/Users/foo → C:\Users\foo
            win_path = remote_sftp.lstrip("/").replace("/", "\\")
            wp = win_path.replace("'", "''")  # escapa aspas simples no contexto PS
            # .bat/.cmd roda via cmd.exe; .ps1 e demais via call direto ao script
            runner = f"cmd.exe /c '{wp}'" if ext in (".bat", ".cmd") else f"& '{wp}'"
            # Usa -EncodedCommand (Base64) para evitar expansão de variáveis pelo shell
            # externo do SSH (cmd.exe ou PowerShell), independente da configuração do servidor.
            import base64 as _b64
            ps_cmd = (
                f"$ProgressPreference='SilentlyContinue'; "
                f"$InformationPreference='SilentlyContinue'; "
                f"$WarningPreference='SilentlyContinue'; "
                f"$VerbosePreference='SilentlyContinue'; "
                f"$rc=1; "
                f"try {{ {runner}; $rc=$LASTEXITCODE }} "
                f"finally {{ Remove-Item -Force '{wp}' -ErrorAction SilentlyContinue }}; "
                f"exit $rc"
            )
            encoded = _b64.b64encode(ps_cmd.encode("utf-16-le")).decode("ascii")
            cmd = f"powershell.exe -ExecutionPolicy Bypass -NonInteractive -NoProfile -EncodedCommand {encoded}"
            stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout * 12, get_pty=pty)
            code = stdout.channel.recv_exit_status()
            out  = stdout.read().decode("utf-8", errors="replace")
            err  = "" if pty else stderr.read().decode("utf-8", errors="replace")
        else:
            remote = f"/tmp/_mssh_{script_name}"
            sftp.put(script_path, remote)
            sftp.chmod(remote, 0o755)
            sftp.close()
            base_cmd = f"bash {remote}"
            if use_sudo:
                base_cmd = _wrap_sudo(client, base_cmd)
            cmd = f"{base_cmd}; _rc=$?; rm -f {remote}; exit $_rc"
            stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout * 12, get_pty=pty)
            if use_sudo and base_cmd.startswith("sudo"):
                _sudo_stdin(stdin, info)
            code = stdout.channel.recv_exit_status()
            out  = stdout.read().decode("utf-8", errors="replace")
            err  = "" if pty else stderr.read().decode("utf-8", errors="replace")
    except Exception as e:
        code, out, err = -1, "", str(e)
    finally:
        if client:
            client.close()
    return name, code, out, err, int((time.monotonic() - t0) * 1000)

# ── Exibição de resultados ────────────────────────────────────────────────────

def show_result(name: str, code: int, out: str, err: str, ms: int = 0):
    timing = f" [dim]{ms}ms[/]" if ms else ""
    if code == 0:
        title, border = f"[green bold]{escape(name)}[/] [dim]exit 0[/]{timing}", "green"
    elif code == -1:
        title, border = f"[red bold]{escape(name)}[/] [red]erro de conexão[/]{timing}", "red"
    else:
        title, border = f"[yellow bold]{escape(name)}[/] [yellow]exit {code}[/]{timing}", "yellow"

    parts = []
    if out.strip():
        parts.append(escape(out.rstrip()))
    if err.strip():
        parts.append(f"[red]{escape(err.rstrip())}[/]")
    body = "\n".join(parts) if parts else "[dim](sem saída)[/]"
    console.print(Panel(body, title=title, border_style=border, padding=(0, 1)))


def show_results(order: list, results: dict, output_file: str = None):
    lines_to_save = []
    for name in order:
        if name not in results:
            continue
        _, code, out, err, ms = results[name]
        show_result(name, code, out, err, ms)
        if output_file:
            lines_to_save.append(f"=== {name} (exit {code}, {ms}ms) ===\n")
            if out.strip():
                lines_to_save.append(out.rstrip() + "\n")
            if err.strip():
                lines_to_save.append("[STDERR]\n" + err.rstrip() + "\n")
            lines_to_save.append("\n")

    if output_file and lines_to_save:
        with open(output_file, "w", encoding="utf-8") as f:
            f.writelines(lines_to_save)
        console.print(f"\n[dim]Saída salva em [bold]{output_file}[/][/]")

# ── Seleção interativa de hosts ───────────────────────────────────────────────

def _tristate_tag_selector(tags: list) -> tuple:
    """
    Seletor de tags com três estados por item:
      ○  (0) = não selecionada
      ●  (1) = OU  — host precisa ter pelo menos uma tag OU
      ★  (2) = E   — host precisa ter esta tag obrigatoriamente

    Teclas: ↑↓ navegar  |  espaço ciclar estado  |  enter confirmar  |  esc cancelar
    Retorna (or_tags, and_tags).
    """
    # Símbolos com fallback ASCII para terminais antigos (Windows cmd legado)
    try:
        "❯●★○".encode(sys.stdout.encoding or "utf-8")
        POINTER = "❯"
        ICONS   = [" ○ ", " ● ", " ★ "]
    except (UnicodeEncodeError, TypeError):
        POINTER = ">"
        ICONS   = ["[ ]", "[o]", "[E]"]

    ICON_STYLES = ["", "class:tag-or", "class:tag-and"]
    HINT = (
        " ○ nenhuma   ● OU   ★ E/obrigatória"
        "   [↑↓] mover   [espaço] ciclar   [enter] confirmar   [esc] cancelar\n\n"
    )

    states: list[int] = [0] * len(tags)
    cursor = 0
    confirmed = False

    kb = KeyBindings()

    @kb.add("up")
    def _up(event):
        nonlocal cursor
        cursor = (cursor - 1) % len(tags)

    @kb.add("down")
    def _down(event):
        nonlocal cursor
        cursor = (cursor + 1) % len(tags)

    @kb.add("space")
    def _cycle(event):
        states[cursor] = (states[cursor] + 1) % 3

    @kb.add("enter")
    def _confirm(event):
        nonlocal confirmed
        confirmed = True
        event.app.exit()

    @kb.add("c-c")
    @kb.add("escape")
    def _cancel(event):
        event.app.exit()

    def get_tokens():
        tokens: list = []
        tokens.append(("class:header", " Selecione as tags:\n"))
        tokens.append(("class:hint", HINT))
        for i, tag in enumerate(tags):
            s = states[i]
            tokens.append(("class:pointer" if i == cursor else "", f" {POINTER} " if i == cursor else "   "))
            tokens.append((ICON_STYLES[s], ICONS[s]))
            tokens.append((ICON_STYLES[s] + " bold" if s else "", tag + "\n"))
        return tokens

    style = PTStyle.from_dict({
        "header":  "bold",
        "hint":    "fg:#858585 italic",
        "pointer": "fg:#5f87ff bold",
        "tag-or":  "fg:#ffaf00 bold",
        "tag-and": "fg:#00d7af bold",
    })

    Application(
        layout=Layout(Window(FormattedTextControl(get_tokens), dont_extend_height=True)),
        key_bindings=kb,
        style=style,
        full_screen=False,
        mouse_support=False,
    ).run()

    if not confirmed:
        return [], []
    return (
        [tags[i] for i, s in enumerate(states) if s == 1],
        [tags[i] for i, s in enumerate(states) if s == 2],
    )


def select_hosts(hosts: dict) -> list:
    if not hosts:
        console.print("[yellow]Nenhum host cadastrado. Use 'add' para cadastrar.[/]")
        return []

    mode = questionary.select(
        "Selecionar hosts por:",
        choices=["Seleção individual", "Tags", "Todos os hosts"],
        style=_style,
    ).ask()

    if mode is None:
        return []

    if mode == "Todos os hosts":
        return list(hosts.keys())

    if mode == "Tags":
        tags = all_tags(hosts)
        if not tags:
            console.print("[yellow]Nenhuma tag definida.[/]")
            return []
        or_tags, and_tags = _tristate_tag_selector(tags)
        if not or_tags and not and_tags:
            return []
        selected = hosts_by_tags_advanced(hosts, or_tags, and_tags)
        parts = []
        if and_tags:
            parts.append(f"[bold cyan]E[/]({', '.join(and_tags)})")
        if or_tags:
            parts.append(f"[bold yellow]OU[/]({', '.join(or_tags)})")
        console.print(f"[dim]Filtro: {' + '.join(parts)} → {len(selected)} host(s)[/]")
        return selected

    # Seleção individual
    choices = []
    for name, h in sorted(hosts.items()):
        tags_str = f"  [{', '.join(h.get('tags', []))}]" if h.get("tags") else ""
        choices.append(Choice(
            title=f"{name}  [dim]{h['user']}@{h['host']}:{h.get('port',22)}[/dim]{tags_str}",
            value=name,
        ))
    return questionary.checkbox(
        "Selecione os hosts (espaço = marcar, enter = confirmar):",
        choices=choices,
        style=_style,
    ).ask() or []

# ── Handlers de subcomandos ───────────────────────────────────────────────────

def do_add(_args=None):
    hosts = load_hosts()
    console.print(Rule("[bold cyan]Cadastrar host[/]"))

    name = questionary.text("Nome (apelido):", style=_style).ask()
    if not name:
        return
    if name in hosts:
        if not questionary.confirm(f"'{name}' já existe. Sobrescrever?", style=_style).ask():
            return

    host = questionary.text("Hostname ou IP:", style=_style).ask()
    if not host:
        return

    port_str = questionary.text("Porta SSH:", default="22", style=_style).ask() or "22"
    try:
        port = int(port_str)
    except ValueError:
        port = 22

    user = questionary.text("Usuário:", style=_style).ask()
    if not user:
        return

    use_pw = questionary.confirm("Autenticar com senha?", default=True, style=_style).ask()
    password = None
    if use_pw:
        password = questionary.password("Senha:", style=_style).ask()

    tags_raw = questionary.text(
        "Tags (separadas por vírgula, ex: web,producao):", style=_style
    ).ask() or ""
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

    hosts[name] = {
        "host": host, "port": port,
        "user": user, "password": password,
        "tags": tags,
    }
    save_hosts(hosts)
    console.print(f"[green]✓[/] Host [bold]{name}[/] salvo.")


def do_edit(_args=None):
    hosts = load_hosts()
    if not hosts:
        console.print("[yellow]Nenhum host cadastrado.[/]")
        return

    name = questionary.select(
        "Selecione o host para editar:",
        choices=sorted(hosts.keys()),
        style=_style,
    ).ask()
    if not name:
        return

    h = hosts[name]
    console.print(Rule(f"[bold cyan]Editar: {name}[/]"))
    console.print("[dim]Pressione Enter para manter o valor atual.[/]\n")

    h["host"] = questionary.text("Hostname ou IP:", default=h["host"], style=_style).ask() or h["host"]
    port_str  = questionary.text("Porta:", default=str(h.get("port", 22)), style=_style).ask() or str(h.get("port", 22))
    h["port"] = int(port_str) if port_str.isdigit() else h.get("port", 22)
    h["user"] = questionary.text("Usuário:", default=h["user"], style=_style).ask() or h["user"]

    if questionary.confirm("Alterar senha/autenticação?", default=False, style=_style).ask():
        use_pw = questionary.confirm("Usar senha?", default=bool(h.get("password")), style=_style).ask()
        h["password"] = questionary.password("Senha:", style=_style).ask() if use_pw else None

    tags_raw  = questionary.text("Tags:", default=",".join(h.get("tags", [])), style=_style).ask() or ""
    h["tags"] = [t.strip() for t in tags_raw.split(",") if t.strip()]

    hosts[name] = h
    save_hosts(hosts)
    console.print(f"[green]✓[/] Host [bold]{name}[/] atualizado.")


def do_list(_args=None):
    hosts = load_hosts()
    if not hosts:
        console.print("[yellow]Nenhum host cadastrado. Use 'add' para cadastrar.[/]")
        return

    t = Table(box=box.ROUNDED, header_style="bold cyan", show_lines=False)
    t.add_column("Nome", style="bold white")
    t.add_column("Host")
    t.add_column("Porta", justify="right", style="dim")
    t.add_column("Usuário", style="cyan")
    t.add_column("Auth", justify="center")
    t.add_column("Tags")

    for name, h in sorted(hosts.items()):
        auth = "[yellow]senha[/]" if h.get("password") else "[green]chave[/]"
        tags = "  ".join(f"[dim cyan]{tag}[/]" for tag in h.get("tags", [])) or "[dim]—[/]"
        t.add_row(name, h["host"], str(h.get("port", 22)), h["user"], auth, tags)

    console.print(t)
    console.print(f"[dim]{len(hosts)} host(s) cadastrado(s). Config: {HOSTS_FILE}[/]")


def do_remove(_args=None):
    hosts = load_hosts()
    if not hosts:
        console.print("[yellow]Nenhum host cadastrado.[/]")
        return

    name = questionary.select(
        "Selecione o host para remover:",
        choices=sorted(hosts.keys()),
        style=_style,
    ).ask()
    if not name:
        return

    if questionary.confirm(f"Remover [bold]{name}[/]?", default=False, style=_style).ask():
        del hosts[name]
        save_hosts(hosts)
        console.print(f"[green]✓[/] Host [bold]{name}[/] removido.")


def do_tags(_args=None):
    hosts = load_hosts()
    tags = all_tags(hosts)
    if not tags:
        console.print("[yellow]Nenhuma tag definida.[/]")
        return

    t = Table(box=box.ROUNDED, header_style="bold cyan")
    t.add_column("Tag", style="bold cyan")
    t.add_column("Hosts")
    t.add_column("#", justify="right", style="dim")

    for tag in tags:
        tagged = sorted(n for n, h in hosts.items() if tag in h.get("tags", []))
        t.add_row(tag, ", ".join(tagged), str(len(tagged)))

    console.print(t)


def do_run(hosts=None, preselected=None, command=None, timeout=DEFAULT_TIMEOUT, output_file=None, pty=False, use_sudo=False):
    if hosts is None:
        hosts = load_hosts()
    if not hosts:
        console.print("[yellow]Nenhum host cadastrado.[/]")
        return

    selected = preselected if preselected is not None else select_hosts(hosts)
    if not selected:
        console.print("[yellow]Nenhum host selecionado.[/]")
        return

    if command is None:
        console.print(f"\nHosts: {', '.join(f'[cyan]{n}[/]' for n in selected)}")
        command = questionary.text("Comando:", style=_style).ask()
    if not command:
        return

    if use_sudo:
        console.print("[yellow bold]Modo sudo ativo — senha enviada automaticamente em hosts Unix.[/]")

    results = {}
    lock = threading.Lock()

    def _run(name):
        r = ssh_run(name, hosts[name], command, timeout, pty, use_sudo)
        with lock:
            results[name] = r

    console.print()
    with console.status(f"[green]Executando em {len(selected)} host(s)...[/]"):
        with ThreadPoolExecutor(max_workers=min(len(selected), 32)) as ex:
            list(as_completed({ex.submit(_run, n): n for n in selected}))

    show_results(selected, results, output_file)
    return results


def do_script(hosts=None, preselected=None, script_path=None, timeout=DEFAULT_TIMEOUT, output_file=None, pty=False, use_sudo=False):
    if hosts is None:
        hosts = load_hosts()
    if not hosts:
        console.print("[yellow]Nenhum host cadastrado.[/]")
        return

    if script_path is None:
        
        # se o timeout for baixo, avisar isso
        if timeout < 120:
            console.print(f"[yellow]Aviso: [red]timeout definido em {timeout}s[yellow]. Garanta que o script termine neste tempo, ou inicie o mulstissh com a flag --timeout mudando este tempo para algo como 1800s (30min).[/]")

        script_path = questionary.path("Arquivo de script:", style=_style).ask()
    if not script_path or not Path(script_path).exists():
        console.print(f"[red]Arquivo não encontrado: {script_path}[/]")
        return

    selected = preselected if preselected is not None else select_hosts(hosts)
    if not selected:
        return

    if use_sudo:
        console.print("[yellow bold]Modo sudo ativo — senha enviada automaticamente em hosts Unix.[/]")

    results = {}
    lock = threading.Lock()

    def _run(name):
        r = ssh_script(name, hosts[name], script_path, timeout, pty, use_sudo)
        with lock:
            results[name] = r

    console.print()
    with console.status(f"[green]Enviando script para {len(selected)} host(s)...[/]"):
        with ThreadPoolExecutor(max_workers=min(len(selected), 32)) as ex:
            list(as_completed({ex.submit(_run, n): n for n in selected}))

    show_results(selected, results, output_file)


def do_test(hosts=None, preselected=None, timeout=DEFAULT_TIMEOUT):
    if hosts is None:
        hosts = load_hosts()
    if not hosts:
        console.print("[yellow]Nenhum host cadastrado.[/]")
        return

    selected = preselected if preselected is not None else select_hosts(hosts)
    if not selected:
        return

    results = {}
    lock = threading.Lock()

    def _run(name):
        r = ssh_run(name, hosts[name], "echo OK", timeout)
        with lock:
            results[name] = r

    console.print()
    with console.status("[green]Testando conexões...[/]"):
        with ThreadPoolExecutor(max_workers=min(len(selected), 32)) as ex:
            list(as_completed({ex.submit(_run, n): n for n in selected}))

    t = Table(box=box.ROUNDED, header_style="bold cyan")
    t.add_column("Host", style="bold")
    t.add_column("Status", justify="center")
    t.add_column("Latência", justify="right")
    t.add_column("Erro")

    ok = fail = 0
    for name in selected:
        _, code, _, err, ms = results.get(name, (name, -1, "", "não testado", 0))
        if code == 0:
            status, ok = "[green]✓ OK[/]", ok + 1
            error = ""
        else:
            status, fail = "[red]✗ FALHA[/]", fail + 1
            error = err.strip()[:70]
        t.add_row(name, status, f"[dim]{ms}ms[/]", error)

    console.print(t)
    console.print(f"[green]{ok} OK[/]  [red]{fail} falha(s)[/]")


def do_repl(_args=None, timeout: int = DEFAULT_TIMEOUT):
    """Sessão interativa: seleciona hosts uma vez, executa vários comandos."""
    hosts = load_hosts()
    if not hosts:
        console.print("[yellow]Nenhum host cadastrado.[/]")
        return

    console.print(Rule("[bold cyan]Sessão interativa[/]"))
    selected = select_hosts(hosts)
    if not selected:
        return

    console.print(f"\nHosts: {', '.join(f'[cyan]{n}[/]' for n in selected)}")
    console.print("[dim]Digite um comando e Enter. Linha vazia = trocar seleção. Ctrl-C = sair.[/]\n")

    while True:
        try:
            cmd = questionary.text("$", style=_style).ask()
        except (KeyboardInterrupt, EOFError):
            break

        if cmd is None:
            break
        if not cmd.strip():
            console.print()
            selected = select_hosts(hosts)
            if not selected:
                break
            console.print(f"Hosts: {', '.join(f'[cyan]{n}[/]' for n in selected)}\n")
            continue

        do_run(hosts, selected, cmd, timeout)
        console.print()

# ── Explorador de arquivos (SFTP) ─────────────────────────────────────────────

def _detect_os_type(client: paramiko.SSHClient) -> str:
    """Detecta se o host remoto é Unix ou Windows via SSH."""
    try:
        _, stdout, _ = client.exec_command("uname -s", timeout=5)
        output = stdout.read().decode("utf-8", errors="replace").strip()
        if output:
            return "unix"
    except Exception:
        pass
    return "windows"


def _sftp_join(base: str, name: str) -> str:
    base = base.rstrip("/")
    return f"{base}/{name}"


def _sftp_parent(path: str) -> str:
    path = path.rstrip("/")
    if "/" not in path:
        return path
    parent, _, _ = path.rpartition("/")
    return parent or "/"


def _fmt_size(size) -> str:
    if size is None:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def _fmt_mtime(mtime) -> str:
    if not mtime:
        return ""
    try:
        return _dt.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _sftp_listdir(sftp, path: str) -> tuple:
    """Retorna (entries, error). entries = lista de dicts com name/is_dir/size/mtime/is_parent."""
    try:
        raw = sftp.listdir_attr(path)
    except Exception as e:
        return None, str(e)

    entries = []
    for attr in raw:
        is_dir = bool(attr.st_mode and _stat_mod.S_ISDIR(attr.st_mode))
        entries.append({
            "name":      attr.filename,
            "is_dir":    is_dir,
            "size":      None if is_dir else attr.st_size,
            "mtime":     attr.st_mtime,
            "is_parent": False,
        })
    entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))

    parent = _sftp_parent(path)
    if parent != path:
        entries.insert(0, {
            "name": "..", "is_dir": True,
            "size": None, "mtime": None, "is_parent": True,
        })

    return entries, None


def _sftp_rmtree(sftp, path: str):
    """Remove recursivamente um arquivo ou diretório via SFTP."""
    attr = sftp.stat(path)
    if _stat_mod.S_ISDIR(attr.st_mode):
        for item in sftp.listdir_attr(path):
            _sftp_rmtree(sftp, _sftp_join(path, item.filename))
        sftp.rmdir(path)
    else:
        sftp.remove(path)


def _sudo_exec(client, cmd: str, password) -> tuple:
    """Executa comando com sudo via SSH. Retorna (stdout, stderr, exit_code)."""
    stdin, stdout, stderr = client.exec_command(cmd, timeout=30)
    if password:
        stdin.write(password + "\n")
        stdin.flush()
    out  = stdout.read().decode("utf-8", errors="replace")
    err  = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    return out, err, code


def _sudo_listdir(client, path: str, password) -> tuple:
    """Lista diretório via sudo find quando SFTP não tem permissão."""
    q   = shlex.quote(path)
    cmd = f"sudo -S -p '' find {q} -maxdepth 1 -mindepth 1 -printf '%y\\t%s\\t%Ts\\t%f\\n' 2>/dev/null"
    out, err, code = _sudo_exec(client, cmd, password)
    if code != 0:
        return None, err.strip() or "Permissão negada (sudo)"

    entries = []
    for line in out.splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        ftype, size_str, mtime_str, name = parts
        if name in (".", ".."):
            continue
        is_dir = ftype == "d"
        try:
            size  = None if is_dir else int(size_str)
            mtime = int(mtime_str) if mtime_str.isdigit() else None
        except ValueError:
            size, mtime = None, None
        entries.append({"name": name, "is_dir": is_dir, "size": size,
                        "mtime": mtime, "is_parent": False})

    entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    parent = _sftp_parent(path)
    if parent != path:
        entries.insert(0, {"name": "..", "is_dir": True,
                           "size": None, "mtime": None, "is_parent": True})
    return entries, None


def _sudo_download(client, remote_src: str, local_dest: str, password) -> str | None:
    """Baixa arquivo via sudo cat. Retorna mensagem de erro ou None em caso de sucesso."""
    import uuid as _uuid
    cmd = f"sudo -S -p '' cat {shlex.quote(remote_src)}"
    stdin, stdout, stderr = client.exec_command(cmd, timeout=120)
    if password:
        stdin.write(password + "\n")
        stdin.flush()
    data = stdout.read()
    code = stdout.channel.recv_exit_status()
    if code != 0:
        return stderr.read().decode("utf-8", errors="replace").strip() or "Erro ao baixar com sudo"
    with open(local_dest, "wb") as f:
        f.write(data)
    return None


def _sudo_rmtree(client, path: str, password) -> str | None:
    """Remove arquivo/diretório via sudo rm -rf. Retorna erro ou None."""
    _, err, code = _sudo_exec(client, f"sudo -S -p '' rm -rf {shlex.quote(path)}", password)
    if code != 0:
        return err.strip() or "Erro ao deletar com sudo"
    return None


def _sudo_upload(sftp, client, local_path: str, remote_dest: str, password) -> str | None:
    """Envia arquivo via SFTP para /tmp e move com sudo. Retorna erro ou None."""
    import uuid as _uuid
    tmp = f"/tmp/_mssh_ul_{_uuid.uuid4().hex[:8]}"
    try:
        sftp.put(local_path, tmp)
    except Exception as e:
        return str(e)
    _, err, code = _sudo_exec(
        client, f"sudo -S -p '' mv {shlex.quote(tmp)} {shlex.quote(remote_dest)}", password
    )
    if code != 0:
        _sudo_exec(client, f"rm -f {shlex.quote(tmp)}", None)
        return err.strip() or "Erro ao mover com sudo"
    return None


def _run_file_browser(sftp, host_name: str, start_path: str, os_type: str,
                      client=None, password=None):
    """TUI interativo de exploração de arquivos via SFTP."""
    VISIBLE = 18
    PAGE    = 10

    current_path = start_path
    entries: list = []
    selected: set = set()   # nomes de arquivos marcados para download
    cursor       = 0
    scroll       = 0
    error_msg    = ""
    status_msg   = ""
    sudo_mode    = False    # True quando o diretório atual só é acessível via sudo

    _can_sudo = (client is not None and os_type == "unix")

    def reload(path=None):
        nonlocal entries, cursor, scroll, error_msg, current_path, selected, sudo_mode
        p = path if path is not None else current_path
        e, err = _sftp_listdir(sftp, p)
        if err:
            if _can_sudo:
                e2, err2 = _sudo_listdir(client, p, password)
                if e2 is not None:
                    current_path = p
                    entries      = e2
                    error_msg    = ""
                    cursor       = 0
                    scroll       = 0
                    selected     = set()
                    sudo_mode    = True
                    return True
            error_msg = err
            return False
        sudo_mode    = False
        current_path = p
        entries      = e
        error_msg    = ""
        cursor       = 0
        scroll       = 0
        selected     = set()
        return True

    reload()

    try:
        "❯📁📄✓".encode(sys.stdout.encoding or "utf-8")
        PTR = "❯"; DICON = "📁"; FICON = "📄"; CHK = "✓"
    except (UnicodeEncodeError, TypeError):
        PTR = ">"; DICON = "[D]"; FICON = "[ ]"; CHK = "*"

    def clamp_scroll():
        nonlocal scroll
        if cursor < scroll:
            scroll = cursor
        elif cursor >= scroll + VISIBLE:
            scroll = cursor - VISIBLE + 1

    def toggle_current():
        """Marca/desmarca o arquivo sob o cursor."""
        nonlocal status_msg
        if not entries:
            return
        entry = entries[cursor]
        if entry["is_dir"]:
            return
        fname = entry["name"]
        if fname in selected:
            selected.discard(fname)
        else:
            selected.add(fname)
        n = len(selected)
        status_msg = f"{n} arquivo(s) selecionado(s)" if n else ""

    kb = KeyBindings()

    @kb.add("up")
    def _up(event):
        nonlocal cursor
        cursor = max(0, cursor - 1)
        clamp_scroll()

    @kb.add("down")
    def _down(event):
        nonlocal cursor
        if entries:
            cursor = min(len(entries) - 1, cursor + 1)
            clamp_scroll()

    @kb.add("pageup")
    @kb.add("c-b")
    def _pgup(event):
        nonlocal cursor
        cursor = max(0, cursor - PAGE)
        clamp_scroll()

    @kb.add("pagedown")
    @kb.add("c-f")
    def _pgdn(event):
        nonlocal cursor
        if entries:
            cursor = min(len(entries) - 1, cursor + PAGE)
            clamp_scroll()

    @kb.add("home")
    def _home(event):
        nonlocal cursor, scroll
        cursor = 0; scroll = 0

    @kb.add("end")
    def _end(event):
        nonlocal cursor
        if entries:
            cursor = len(entries) - 1
            clamp_scroll()

    @kb.add("enter")
    @kb.add("right")
    def _enter(event):
        nonlocal status_msg
        if not entries:
            return
        entry = entries[cursor]
        if entry["is_dir"]:
            new_path = (_sftp_parent(current_path) if entry["is_parent"]
                        else _sftp_join(current_path, entry["name"]))
            if reload(new_path):
                status_msg = ""
        else:
            toggle_current()

    @kb.add("space")
    def _space(event):
        toggle_current()

    @kb.add("backspace")
    @kb.add("left")
    def _back(event):
        nonlocal status_msg
        parent = _sftp_parent(current_path)
        if parent == current_path:
            return
        if reload(parent):
            status_msg = ""

    @kb.add("d")
    @kb.add("D")
    def _download(event):
        nonlocal status_msg
        if not entries:
            return
        # Baixa os marcados; se nenhum marcado, baixa o que está sob o cursor
        to_dl = list(selected) if selected else None
        if to_dl is None:
            entry = entries[cursor]
            if entry["is_dir"]:
                status_msg = "Navegue até um arquivo e pressione Espaço/Enter para marcar ou D para baixar direto"
                return
            to_dl = [entry["name"]]
        event.app.exit(result=("download", to_dl))

    @kb.add("u")
    @kb.add("U")
    def _upload(event):
        event.app.exit(result=("upload",))

    @kb.add("delete")
    def _delete(event):
        nonlocal status_msg
        if not entries:
            return
        # Arquivos marcados → deleta todos; senão deleta o item sob o cursor
        if selected:
            to_del = list(selected)
        else:
            entry = entries[cursor]
            if entry["is_parent"]:
                status_msg = "Não é possível deletar o diretório pai"
                return
            to_del = [entry["name"]]
        event.app.exit(result=("delete", to_del))

    @kb.add("r")
    @kb.add("R")
    def _refresh(event):
        nonlocal status_msg
        reload()
        status_msg = "Diretório atualizado"

    @kb.add("q")
    @kb.add("Q")
    @kb.add("c-c")
    @kb.add("escape")
    def _quit(event):
        event.app.exit(result=None)

    def get_tokens():
        toks = []
        toks.append(("class:title", f" {host_name}  "))
        toks.append(("class:os",    f"[{os_type}]"))
        if sudo_mode:
            toks.append(("class:sudo", "  [sudo]"))
        if selected:
            toks.append(("class:sel-n", f"  [{len(selected)} selecionado(s)]"))
        toks.append(("", "\n"))
        toks.append(("class:path",  f" {current_path}\n"))
        toks.append(("class:sep",   "─" * 72 + "\n"))

        if error_msg:
            toks.append(("class:err", f" Erro: {error_msg}\n"))
        elif not entries:
            toks.append(("class:dim", " (diretório vazio)\n"))
        else:
            for i in range(scroll, min(scroll + VISIBLE, len(entries))):
                entry  = entries[i]
                is_cur = (i == cursor)
                ptr    = PTR if is_cur else " "

                if entry["is_dir"]:
                    icon  = DICON
                    mark  = "   "
                    ntext = ".. (subir)" if entry["is_parent"] else entry["name"] + "/"
                    extra = ""
                    sty   = "class:dir-c" if is_cur else "class:dir"
                else:
                    is_chk = entry["name"] in selected
                    icon   = FICON
                    mark   = f"[{CHK}]" if is_chk else "[ ]"
                    ntext  = entry["name"]
                    extra  = f"  {_fmt_size(entry['size']):>10}   {_fmt_mtime(entry['mtime'])}"
                    if is_chk:
                        sty = "class:chk-c" if is_cur else "class:chk"
                    else:
                        sty = "class:fil-c" if is_cur else "class:fil"

                toks.append((sty, f" {ptr} {icon} {mark} {ntext:<41}{extra}\n"))

            if len(entries) > VISIBLE:
                toks.append(("class:dim", f"   [{cursor + 1}/{len(entries)}]\n"))

        toks.append(("class:sep", "─" * 72 + "\n"))
        if status_msg:
            toks.append(("class:sts", f" {status_msg}\n"))
        toks.append(("class:hint",
            " Espaço marcar   D baixar   U enviar   Del deletar   R atualizar   Q sair\n"
        ))
        return toks

    pt_style = PTStyle.from_dict({
        "title": "bold cyan",
        "os":    "fg:#666666",
        "sudo":  "fg:#ffaf00 bold",
        "sel-n": "fg:#00af5f bold",
        "path":  "fg:#ffaf00 bold",
        "sep":   "fg:#3a3a3a",
        "dir":   "fg:#5f87ff",
        "dir-c": "fg:#ffffff bg:#5f87ff bold",
        "fil":   "",
        "fil-c": "reverse bold",
        "chk":   "fg:#00af5f",
        "chk-c": "fg:#00af5f reverse bold",
        "hint":  "fg:#666666 italic",
        "err":   "fg:#ff5555 bold",
        "dim":   "fg:#666666",
        "sts":   "fg:#00af5f bold",
    })

    while True:
        result = Application(
            layout=Layout(Window(FormattedTextControl(get_tokens), dont_extend_height=True)),
            key_bindings=kb,
            style=pt_style,
            full_screen=False,
            mouse_support=False,
        ).run()

        if result is None:
            break

        status_msg = ""
        action     = result[0]

        if action == "upload":
            console.print()
            console.print("[bold cyan]Enviar arquivo[/]  (caminho completo no sistema local)")
            console.print("[dim]Dica: use Tab para autocompletar no shell antes de colar aqui[/]")
            try:
                local_raw = input("  Arquivo local: ").strip()
            except (EOFError, KeyboardInterrupt):
                local_raw = ""
            if local_raw:
                lp = Path(local_raw).expanduser()
                if lp.is_file():
                    remote_dest = _sftp_join(current_path, lp.name)
                    console.print(f"[dim]Enviando {lp.name} → {remote_dest} ...[/]")
                    if sudo_mode:
                        err = _sudo_upload(sftp, client, str(lp), remote_dest, password)
                    else:
                        err = None
                        try:
                            sftp.put(str(lp), remote_dest)
                        except Exception as ex:
                            err = str(ex)
                    if err:
                        console.print(f"[red]Erro ao enviar: {err}[/]")
                        status_msg = "✗ Erro ao enviar"
                    else:
                        console.print("[green]✓ Enviado com sucesso[/]")
                        status_msg = f"✓ Enviado: {lp.name}"
                        reload()
                else:
                    console.print(f"[red]Arquivo não encontrado: {lp}[/]")
                    status_msg = "✗ Arquivo não encontrado"

        elif action == "download":
            filenames = result[1]   # lista de nomes
            n = len(filenames)
            console.print()
            console.print(f"[bold cyan]Baixar {n} arquivo(s)[/]: {', '.join(filenames[:3])}{'...' if n > 3 else ''}")
            dl_default = str(Path.home() / "Downloads")
            console.print(f"[dim]Diretório destino (Enter = {dl_default}):[/]")
            try:
                dest_raw = input("  Destino: ").strip() or dl_default
            except (EOFError, KeyboardInterrupt):
                dest_raw = ""
            if dest_raw:
                dest_dir = Path(dest_raw).expanduser()
                dest_dir.mkdir(parents=True, exist_ok=True)
                errs = []
                for fname in filenames:
                    remote_src = _sftp_join(current_path, fname)
                    dest_path  = str(dest_dir / fname)
                    if sudo_mode:
                        err = _sudo_download(client, remote_src, dest_path, password)
                        if err:
                            errs.append(fname)
                            console.print(f"[red]✗ {fname}: {err}[/]")
                        else:
                            console.print(f"[green]✓ {fname}[/]")
                    else:
                        try:
                            sftp.get(remote_src, dest_path)
                            console.print(f"[green]✓ {fname}[/]")
                        except Exception as ex:
                            errs.append(fname)
                            console.print(f"[red]✗ {fname}: {ex}[/]")
                if errs:
                    status_msg = f"✗ {len(errs)} erro(s)"
                else:
                    status_msg = f"✓ {n} arquivo(s) baixado(s) em {dest_dir}"
                selected.clear()

        elif action == "delete":
            names = result[1]
            n     = len(names)
            console.print()
            preview = ", ".join(names[:5]) + ("..." if n > 5 else "")
            console.print(f"[bold red]Deletar {n} item(s) no host remoto:[/] {preview}")
            console.print("[red bold]Esta ação é IRREVERSÍVEL e pode remover diretórios inteiros.[/]")
            try:
                confirm = input("  Confirma? [s/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                confirm = ""
            if confirm in ("s", "sim", "y", "yes"):
                errs = []
                for name in names:
                    path = _sftp_join(current_path, name)
                    if sudo_mode:
                        err = _sudo_rmtree(client, path, password)
                        if err:
                            errs.append(name)
                            console.print(f"[red]✗ {name}: {err}[/]")
                        else:
                            console.print(f"[green]✓ Deletado: {name}[/]")
                    else:
                        try:
                            _sftp_rmtree(sftp, path)
                            console.print(f"[green]✓ Deletado: {name}[/]")
                        except Exception as ex:
                            errs.append(name)
                            console.print(f"[red]✗ {name}: {ex}[/]")
                if errs:
                    status_msg = f"✗ {len(errs)} erro(s) ao deletar"
                else:
                    status_msg = f"✓ {n} item(s) deletado(s)"
                selected.clear()
                reload()
            else:
                status_msg = "Deleção cancelada"


def do_explore(host_name: str = None, timeout: int = DEFAULT_TIMEOUT):
    """Explorador de arquivos interativo via SFTP (Linux e Windows)."""
    hosts = load_hosts()
    if not hosts:
        console.print("[yellow]Nenhum host cadastrado.[/]")
        return

    if not host_name:
        choices = []
        for name, h in sorted(hosts.items()):
            tags_str = f"  [{', '.join(h.get('tags', []))}]" if h.get("tags") else ""
            choices.append(Choice(
                title=f"{name}  [dim]{h['user']}@{h['host']}:{h.get('port', 22)}[/dim]{tags_str}",
                value=name,
            ))
        host_name = questionary.select(
            "Explorar arquivos de qual host?",
            choices=choices,
            style=_style,
        ).ask()
        if not host_name:
            return

    info = hosts[host_name]
    console.print(f"Conectando a [bold cyan]{host_name}[/]...")

    try:
        client = _connect(info, timeout)
    except Exception as e:
        console.print(f"[red]Erro ao conectar: {e}[/]")
        return

    sftp = None
    try:
        sftp    = client.open_sftp()
        os_type = _detect_os_type(client)
        try:
            start = sftp.normalize(".")
        except Exception:
            start = "/"
        console.print(f"[dim]SO detectado: {os_type}  |  Diretório inicial: {start}[/]\n")
        _run_file_browser(sftp, host_name, start, os_type, client, info.get("password"))
    except Exception as e:
        console.print(f"[red]Erro SFTP: {e}[/]")
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass
        client.close()

    console.print("[dim]Explorador encerrado.[/]")


# ── Conexão SSH interativa ────────────────────────────────────────────────────

def _build_ssh_argv(info: dict) -> list:
    ssh_bin = shutil.which("ssh") or "ssh"
    return [ssh_bin, "-p", str(info.get("port", 22)), f"{info['user']}@{info['host']}"]


def _try_open_new_tab(ssh_argv: list) -> bool:
    """Tenta abrir ssh em nova guia/janela de terminal. Retorna True se conseguiu."""
    # tmux: abre nova janela na sessão atual
    if os.environ.get("TMUX") and shutil.which("tmux"):
        try:
            subprocess.Popen(["tmux", "new-window"] + ssh_argv)
            return True
        except Exception:
            pass

    cmd_str = shlex.join(ssh_argv)

    # Terminais gráficos comuns no Linux
    candidates: list[list[str]] = []
    if shutil.which("gnome-terminal"):
        candidates.append(["gnome-terminal", "--tab", "--"] + ssh_argv)
    if shutil.which("konsole"):
        candidates.append(["konsole", "--new-tab", "-e"] + ssh_argv)
    if shutil.which("xfce4-terminal"):
        candidates.append(["xfce4-terminal", "--tab", "-e", cmd_str])
    if shutil.which("tilix"):
        candidates.append(["tilix", "--action=app-new-session", "-e"] + ssh_argv)
    if shutil.which("xterm"):
        candidates.append(["xterm", "-e"] + ssh_argv)

    for cmd in candidates:
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            continue

    return False


def do_connect(_args=None, host_name: str = None):
    """Abre sessão SSH interativa em um host (nova guia se possível)."""
    hosts = load_hosts()
    if not hosts:
        console.print("[yellow]Nenhum host cadastrado.[/]")
        return

    if host_name and host_name not in hosts:
        console.print(f"[red]Host '{host_name}' não encontrado.[/]")
        return

    if not host_name:
        choices = []
        for name, h in sorted(hosts.items()):
            tags_str = f"  [{', '.join(h.get('tags', []))}]" if h.get("tags") else ""
            choices.append(Choice(
                title=f"{name}  [dim]{h['user']}@{h['host']}:{h.get('port',22)}[/dim]{tags_str}",
                value=name,
            ))
        host_name = questionary.select(
            "Conectar a qual host?",
            choices=choices,
            style=_style,
        ).ask()
        if not host_name:
            return

    info = hosts[host_name]
    ssh_argv = _build_ssh_argv(info)

    console.print(
        f"\nAbrindo SSH para [bold cyan]{host_name}[/] "
        f"([dim]{info['user']}@{info['host']}:{info.get('port', 22)}[/dim])..."
    )

    if info.get("password"):
        # Com senha, fica no terminal atual para o usuário digitar normalmente
        console.print("[dim]Conectando no terminal atual (autenticação por senha).[/]\n")
        subprocess.call(ssh_argv)
    elif _try_open_new_tab(ssh_argv):
        console.print("[dim]Sessão SSH aberta em nova guia.[/]")
    else:
        console.print("[dim]Nova guia não disponível — conectando no terminal atual.[/]\n")
        subprocess.call(ssh_argv)
    console.print()


# ── Menu interativo principal ─────────────────────────────────────────────────

def interactive_menu(timeout: int = DEFAULT_TIMEOUT, use_sudo: bool = False):
    header_extra = "  [yellow bold][sudo][/]" if use_sudo else ""
    console.print(Panel.fit(
        f"[bold cyan]multi-ssh[/]{header_extra}\n"
        "[dim]Execute comandos SSH em múltiplos hosts ao mesmo tempo[/]",
        border_style="cyan",
        padding=(0, 2),
    ))

    while True:
        choice = questionary.select(
            "O que deseja fazer?",
            choices=[
                Choice("Executar comando nos hosts",        "run"),
                Choice("Sessão interativa (multi-comando)", "repl"),
                Choice("Conectar a um host (SSH direto)",   "connect"),
                Choice("Explorar arquivos de um host",      "explore"),
                Choice("Executar script nos hosts",         "script"),
                Choice("Testar conectividade SSH",          "test"),
                Separator("─── Gerenciar hosts ───"),
                Choice("Adicionar host",    "add"),
                Choice("Editar host",       "edit"),
                Choice("Listar hosts",      "list"),
                Choice("Remover host",      "remove"),
                Choice("Listar tags",       "tags"),
                Separator(),
                Choice("Sair", "exit"),
            ],
            style=_style,
        ).ask()

        if choice is None or choice == "exit":
            break

        console.print()
        {
            "run":     lambda: do_run(timeout=timeout, use_sudo=use_sudo),
            "repl":    lambda: do_repl(timeout=timeout),
            "connect": do_connect,
            "explore": lambda: do_explore(timeout=timeout),
            "script":  lambda: do_script(timeout=timeout, use_sudo=use_sudo),
            "test":    lambda: do_test(timeout=timeout),
            "add":     do_add,
            "edit":    do_edit,
            "list":    do_list,
            "remove":  do_remove,
            "tags":    do_tags,
        }[choice]()
        console.print()

# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="multissh",
        description="Execute comandos SSH em múltiplos hosts simultaneamente.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
subcomandos:
  add              Adiciona um host (interativo)
  edit             Edita um host existente
  list             Lista todos os hosts
  remove           Remove um host
  tags             Exibe tags e seus hosts
  run [CMD]        Executa comando nos hosts selecionados
  script ARQUIVO   Envia e executa um script shell
  test             Testa conectividade SSH
  repl             Sessão interativa (mantém seleção de hosts)
  connect [NOME]   Abre sessão SSH interativa em um host (nova guia se possível)
  explore [NOME]   Explorador de arquivos via SFTP (Linux e Windows)

exemplos:
  multissh                        # menu interativo
  multissh add                    # cadastra um host
  multissh list                   # lista hosts cadastrados
  multissh run "uptime"           # executa em hosts selecionados
  multissh run --tag web "df -h"  # executa nos hosts com tag 'web'
  multissh run --all "whoami"     # executa em todos os hosts
  multissh script deploy.sh --tag producao
  multissh test --all             # testa todos os hosts
  multissh connect webserver1     # conecta diretamente ao host 'webserver1'
  multissh explore webserver1     # explorador de arquivos no host 'webserver1'
  multissh explore                # seleciona host interativamente
        """,
    )
    p.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT, metavar="SEG",
        help=f"Timeout de conexão SSH em segundos (padrão: {DEFAULT_TIMEOUT})",
    )
    p.add_argument(
        "--sudo", action="store_true", dest="use_sudo",
        help="Executa comandos com sudo (usa senha armazenada; ignorado em hosts Windows)",
    )

    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("add",    help="Adiciona um host")
    sub.add_parser("edit",   help="Edita um host")
    sub.add_parser("list",   help="Lista hosts")
    sub.add_parser("remove", help="Remove um host")
    sub.add_parser("tags",   help="Lista tags")
    sub.add_parser("repl",   help="Sessão interativa")

    con_p = sub.add_parser("connect", help="Abre sessão SSH interativa em um host")
    con_p.add_argument("name", nargs="?", help="Nome do host (opcional)")

    exp_p = sub.add_parser("explore", help="Explorador de arquivos via SFTP")
    exp_p.add_argument("name", nargs="?", help="Nome do host (opcional)")

    def add_host_filters(sp):
        sp.add_argument("--tag", dest="tags", action="append", metavar="TAG",
                        help="Filtra hosts por tag (pode repetir)")
        sp.add_argument("--all", dest="all_hosts", action="store_true",
                        help="Usa todos os hosts")

    run_p = sub.add_parser("run", help="Executa um comando")
    run_p.add_argument("command", nargs="*", help="Comando a executar")
    run_p.add_argument("--output", metavar="ARQUIVO", help="Salva saída em arquivo")
    run_p.add_argument("--pty", action="store_true", help="Aloca PTY (útil para sudo e comandos que exigem TTY)")
    run_p.add_argument("--sudo", action="store_true", dest="use_sudo", help="Executa com sudo (usa senha armazenada; ignorado em hosts Windows)")
    add_host_filters(run_p)

    scr_p = sub.add_parser("script", help="Envia e executa um script")
    scr_p.add_argument("file", nargs="?", help="Caminho do script")
    scr_p.add_argument("--output", metavar="ARQUIVO", help="Salva saída em arquivo")
    scr_p.add_argument("--pty", action="store_true", help="Aloca PTY (útil para sudo e comandos que exigem TTY)")
    scr_p.add_argument("--sudo", action="store_true", dest="use_sudo", help="Executa com sudo (usa senha armazenada; ignorado em hosts Windows)")
    add_host_filters(scr_p)

    tst_p = sub.add_parser("test", help="Testa conectividade")
    add_host_filters(tst_p)

    return p


def resolve_hosts_from_args(hosts: dict, args) -> list | None:
    if getattr(args, "all_hosts", False):
        return list(hosts.keys())
    if getattr(args, "tags", None):
        return hosts_by_tags(hosts, args.tags)
    return None  # None = pedir interativamente


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.cmd:
        try:
            interactive_menu(args.timeout, getattr(args, "use_sudo", False))
        except KeyboardInterrupt:
            console.print("\n[dim]Saindo.[/]")
        return

    handlers = {
        "add":     lambda: do_add(),
        "edit":    lambda: do_edit(),
        "list":    lambda: do_list(),
        "remove":  lambda: do_remove(),
        "tags":    lambda: do_tags(),
        "repl":    lambda: do_repl(),
        "connect": lambda: do_connect(host_name=getattr(args, "name", None)),
        "explore": lambda: do_explore(host_name=getattr(args, "name", None), timeout=args.timeout),
    }

    if args.cmd in handlers:
        try:
            handlers[args.cmd]()
        except KeyboardInterrupt:
            console.print("\n[dim]Cancelado.[/]")
        return

    hosts = load_hosts()
    pre   = resolve_hosts_from_args(hosts, args)

    try:
        if args.cmd == "run":
            cmd = " ".join(args.command) if args.command else None
            do_run(hosts, pre, cmd, args.timeout, getattr(args, "output", None), getattr(args, "pty", False), getattr(args, "use_sudo", False))

        elif args.cmd == "script":
            do_script(hosts, pre, getattr(args, "file", None), args.timeout, getattr(args, "output", None), getattr(args, "pty", False), getattr(args, "use_sudo", False))

        elif args.cmd == "test":
            do_test(hosts, pre, args.timeout)

    except KeyboardInterrupt:
        console.print("\n[dim]Interrompido.[/]")


if __name__ == "__main__":
    main()
