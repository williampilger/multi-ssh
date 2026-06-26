# multi-ssh

> ⚠️ Aplicativo de terminal desenvolvido com **uso pesado** de geração de código por IA (Claude Code).
> Embora o código tenha sido revisado e ajustado, use por sua conta e risco.

> ⚠️ Eu uso esse app constantemente em um **Ubuntu 25.01**, então é nesse sistema que ele funciona adequadamente.
> Em Windows deve funcionar? Sim, deve. Mas, as minhas alterações não são testadas sempre nele para validar.

Execute comandos SSH em múltiplos hosts simultaneamente.

Cadastre seus servidores com nome, IP, porta, usuário, senha e **tags** para agrupá-los.
Selecione um ou mais hosts (ou grupos por tag) e rode comandos ou scripts em todos de uma vez — em paralelo.

---

## Instalação

### Linux / macOS

```bash
git clone https://github.com/williampilger/multi-ssh
cd multi-ssh
chmod +x install.sh
./install.sh
```

Depois de instalar, abra um novo terminal (ou rode `export PATH="$HOME/.local/bin:$PATH"`) e execute:

```bash
multissh
```

### Windows

1. Tenha o [Python 3.9+](https://www.python.org/downloads/) instalado (marque "Add Python to PATH").
2. Execute `install.bat` como usuário normal (não precisa de administrador).
3. Abra um novo terminal (cmd ou PowerShell) e execute `multissh`.

### Sem instalar (modo direto)

Se preferir não instalar, instale as dependências manualmente e rode o script diretamente:

```bash
pip install paramiko questionary rich
python multissh.py
```

---

## Uso

### Menu interativo (recomendado)

Sem argumentos, abre um menu navegável com teclado:

```bash
multissh
```

### Linha de comando

```bash
# Gerenciar hosts
multissh add              # cadastra novo host (interativo)
multissh list             # lista todos os hosts
multissh edit             # edita um host
multissh remove           # remove um host
multissh tags             # lista tags e quais hosts têm cada uma

# Executar comandos
multissh run              # seleciona hosts e digita o comando
multissh run "uptime"             # digita o comando, seleciona hosts
multissh run --tag web "df -h"    # roda nos hosts com tag 'web'
multissh run --tag web --tag db "free -h"  # tags combinadas (OR)
multissh run --all "hostname"     # roda em todos os hosts

# Executar script local nos hosts remotos
multissh script deploy.sh
multissh script deploy.sh --tag producao

# Testar conectividade
multissh test             # testa hosts selecionados
multissh test --all       # testa todos

# Sessão interativa (seleciona hosts uma vez, roda vários comandos)
multissh repl

# Salvar saída em arquivo
multissh run --all "df -h" --output relatorio.txt

# Timeout personalizado (padrão: 15s)
multissh run --all "apt update" --timeout 60
```

---

## Onde ficam os dados

| Sistema | Caminho                                    |
|---------|--------------------------------------------|
| Linux   | `~/.config/multi-ssh/hosts.json`           |
| Windows | `%APPDATA%\multi-ssh\hosts.json` (via venv)|

O arquivo tem permissão `600` no Linux (somente o dono lê). **As senhas ficam em texto claro neste arquivo** — proteja o acesso à sua máquina.

---

## Autenticação

- **Senha**: informe durante o `add`. Fica armazenada no `hosts.json`.
- **Chave SSH**: deixe a senha em branco. O `multissh` usará automaticamente o agente SSH (`ssh-agent`) e os arquivos padrão (`~/.ssh/id_rsa`, `~/.ssh/id_ed25519`, etc.).

---

## Exemplo de sessão

```
$ multissh

  multi-ssh
  Execute comandos SSH em múltiplos hosts ao mesmo tempo

? O que deseja fazer?  Executar comando nos hosts
? Selecionar hosts por:  Tags
? Selecione as tags:  ● producao

Hosts: web01, web02, db01

$ uptime

╭─ web01  exit 0  312ms ──────────────────────╮
│  14:23:01 up 42 days, 3 users, load: 0.12   │
╰──────────────────────────────────────────────╯
╭─ web02  exit 0  398ms ──────────────────────╮
│  14:23:01 up 42 days, 2 users, load: 0.08   │
╰──────────────────────────────────────────────╯
╭─ db01   exit 0  441ms ──────────────────────╮
│  14:23:01 up 15 days, 1 user,  load: 1.45   │
╰──────────────────────────────────────────────╯
```

---

## Dependências

| Pacote       | Para quê                          |
|--------------|-----------------------------------|
| `paramiko`   | Conexão SSH                       |
| `questionary`| Menus e seleção interativa        |
| `rich`       | Output colorido e formatado       |

Todas instaladas automaticamente pelo script de instalação.
