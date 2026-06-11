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
"""

import json
import os
import sys
import threading
import argparse
from pathlib import Path
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

# ── Configuração ──────────────────────────────────────────────────────────────

CONFIG_DIR      = Path.home() / ".config" / "multi-ssh"
HOSTS_FILE      = CONFIG_DIR / "hosts.json"
DEFAULT_TIMEOUT = 15   # segundos para conexão SSH

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


def ssh_run(name: str, info: dict, command: str, timeout: int = DEFAULT_TIMEOUT) -> tuple:
    """Retorna (name, exit_code, stdout, stderr, elapsed_ms)."""
    t0 = time.monotonic()
    client = None
    try:
        client = _connect(info, timeout)
        _, stdout, stderr = client.exec_command(command, timeout=timeout * 6)
        code = stdout.channel.recv_exit_status()
        out  = stdout.read().decode("utf-8", errors="replace")
        err  = stderr.read().decode("utf-8", errors="replace")
    except Exception as e:
        code, out, err = -1, "", str(e)
    finally:
        if client:
            client.close()
    return name, code, out, err, int((time.monotonic() - t0) * 1000)


def ssh_script(name: str, info: dict, script_path: str, timeout: int = DEFAULT_TIMEOUT) -> tuple:
    """Envia e executa um script shell. Retorna (name, exit_code, stdout, stderr, elapsed_ms)."""
    t0 = time.monotonic()
    client = None
    try:
        client = _connect(info, timeout)
        remote = f"/tmp/_mssh_{Path(script_path).name}"
        sftp = client.open_sftp()
        sftp.put(script_path, remote)
        sftp.chmod(remote, 0o755)
        sftp.close()
        cmd = f"bash {remote}; _rc=$?; rm -f {remote}; exit $_rc"
        _, stdout, stderr = client.exec_command(cmd, timeout=timeout * 12)
        code = stdout.channel.recv_exit_status()
        out  = stdout.read().decode("utf-8", errors="replace")
        err  = stderr.read().decode("utf-8", errors="replace")
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
        chosen = questionary.checkbox(
            "Selecione as tags (espaço = marcar, enter = confirmar):",
            choices=tags,
            style=_style,
        ).ask()
        if not chosen:
            return []
        return hosts_by_tags(hosts, chosen)

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


def do_run(hosts=None, preselected=None, command=None, timeout=DEFAULT_TIMEOUT, output_file=None):
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

    results = {}
    lock = threading.Lock()

    def _run(name):
        r = ssh_run(name, hosts[name], command, timeout)
        with lock:
            results[name] = r

    console.print()
    with console.status(f"[green]Executando em {len(selected)} host(s)...[/]"):
        with ThreadPoolExecutor(max_workers=min(len(selected), 32)) as ex:
            list(as_completed({ex.submit(_run, n): n for n in selected}))

    show_results(selected, results, output_file)
    return results


def do_script(hosts=None, preselected=None, script_path=None, timeout=DEFAULT_TIMEOUT, output_file=None):
    if hosts is None:
        hosts = load_hosts()
    if not hosts:
        console.print("[yellow]Nenhum host cadastrado.[/]")
        return

    if script_path is None:
        script_path = questionary.path("Arquivo de script:", style=_style).ask()
    if not script_path or not Path(script_path).exists():
        console.print(f"[red]Arquivo não encontrado: {script_path}[/]")
        return

    selected = preselected if preselected is not None else select_hosts(hosts)
    if not selected:
        return

    results = {}
    lock = threading.Lock()

    def _run(name):
        r = ssh_script(name, hosts[name], script_path, timeout)
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


def do_repl(_args=None):
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

        do_run(hosts, selected, cmd)
        console.print()

# ── Menu interativo principal ─────────────────────────────────────────────────

def interactive_menu():
    console.print(Panel.fit(
        "[bold cyan]multi-ssh[/]\n"
        "[dim]Execute comandos SSH em múltiplos hosts ao mesmo tempo[/]",
        border_style="cyan",
        padding=(0, 2),
    ))

    while True:
        choice = questionary.select(
            "O que deseja fazer?",
            choices=[
                Choice("Executar comando nos hosts",      "run"),
                Choice("Sessão interativa (multi-comando)", "repl"),
                Choice("Executar script nos hosts",        "script"),
                Choice("Testar conectividade SSH",         "test"),
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
            "run":    do_run,
            "repl":   do_repl,
            "script": do_script,
            "test":   do_test,
            "add":    do_add,
            "edit":   do_edit,
            "list":   do_list,
            "remove": do_remove,
            "tags":   do_tags,
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

exemplos:
  multissh                        # menu interativo
  multissh add                    # cadastra um host
  multissh list                   # lista hosts cadastrados
  multissh run "uptime"           # executa em hosts selecionados
  multissh run --tag web "df -h"  # executa nos hosts com tag 'web'
  multissh run --all "whoami"     # executa em todos os hosts
  multissh script deploy.sh --tag producao
  multissh test --all             # testa todos os hosts
        """,
    )
    p.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT, metavar="SEG",
        help=f"Timeout de conexão SSH em segundos (padrão: {DEFAULT_TIMEOUT})",
    )

    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("add",    help="Adiciona um host")
    sub.add_parser("edit",   help="Edita um host")
    sub.add_parser("list",   help="Lista hosts")
    sub.add_parser("remove", help="Remove um host")
    sub.add_parser("tags",   help="Lista tags")
    sub.add_parser("repl",   help="Sessão interativa")

    def add_host_filters(sp):
        sp.add_argument("--tag", dest="tags", action="append", metavar="TAG",
                        help="Filtra hosts por tag (pode repetir)")
        sp.add_argument("--all", dest="all_hosts", action="store_true",
                        help="Usa todos os hosts")

    run_p = sub.add_parser("run", help="Executa um comando")
    run_p.add_argument("command", nargs="*", help="Comando a executar")
    run_p.add_argument("--output", metavar="ARQUIVO", help="Salva saída em arquivo")
    add_host_filters(run_p)

    scr_p = sub.add_parser("script", help="Envia e executa um script")
    scr_p.add_argument("file", nargs="?", help="Caminho do script")
    scr_p.add_argument("--output", metavar="ARQUIVO", help="Salva saída em arquivo")
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
            interactive_menu()
        except KeyboardInterrupt:
            console.print("\n[dim]Saindo.[/]")
        return

    handlers = {
        "add":    lambda: do_add(),
        "edit":   lambda: do_edit(),
        "list":   lambda: do_list(),
        "remove": lambda: do_remove(),
        "tags":   lambda: do_tags(),
        "repl":   lambda: do_repl(),
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
            do_run(hosts, pre, cmd, args.timeout, getattr(args, "output", None))

        elif args.cmd == "script":
            do_script(hosts, pre, getattr(args, "file", None), args.timeout, getattr(args, "output", None))

        elif args.cmd == "test":
            do_test(hosts, pre, args.timeout)

    except KeyboardInterrupt:
        console.print("\n[dim]Interrompido.[/]")


if __name__ == "__main__":
    main()
