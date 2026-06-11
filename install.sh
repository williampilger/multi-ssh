#!/usr/bin/env bash
# Instalador do multi-ssh para Linux/macOS
set -euo pipefail

INSTALL_DIR="$HOME/.local/share/multi-ssh"
BIN_DIR="$HOME/.local/bin"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

info()    { echo -e "${CYAN}[multi-ssh]${NC} $*"; }
success() { echo -e "${GREEN}[multi-ssh]${NC} $*"; }
warn()    { echo -e "${YELLOW}[multi-ssh]${NC} $*"; }
error()   { echo -e "${RED}[multi-ssh]${NC} $*" >&2; exit 1; }

# Verifica Python 3.9+
if ! command -v python3 &>/dev/null; then
    error "Python 3 não encontrado. Instale com: sudo apt install python3"
fi

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [[ "$PY_MAJOR" -lt 3 || ( "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 9 ) ]]; then
    error "Python 3.9 ou superior necessário (encontrado: $PY_VER)"
fi
info "Python $PY_VER encontrado."

# Cria diretórios
mkdir -p "$INSTALL_DIR" "$BIN_DIR"

# Copia o script principal
cp "$SCRIPT_DIR/multissh.py" "$INSTALL_DIR/multissh.py"
chmod +x "$INSTALL_DIR/multissh.py"

# Cria ambiente virtual
info "Criando ambiente virtual..."
python3 -m venv "$INSTALL_DIR/venv"

# Instala dependências
info "Instalando dependências (paramiko, questionary, rich)..."
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet paramiko questionary rich

# Cria script launcher
cat > "$BIN_DIR/multissh" << EOF
#!/usr/bin/env bash
exec "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/multissh.py" "\$@"
EOF
chmod +x "$BIN_DIR/multissh"

# Adiciona ~/.local/bin ao PATH se necessário
PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
ADDED_PATH=false

for rc_file in "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.zshrc" "$HOME/.profile"; do
    if [[ -f "$rc_file" ]] && ! grep -qF 'local/bin' "$rc_file"; then
        echo "" >> "$rc_file"
        echo "# multi-ssh" >> "$rc_file"
        echo "$PATH_LINE" >> "$rc_file"
        warn "Adicionado ao PATH em $rc_file"
        ADDED_PATH=true
        break
    fi
done

echo ""
success "Instalação concluída!"
echo ""
echo "  Comando instalado: $BIN_DIR/multissh"
echo "  Dados:             $INSTALL_DIR"
echo "  Config/hosts:      ~/.config/multi-ssh/hosts.json"
echo ""

if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    warn "Execute o comando abaixo para usar 'multissh' agora:"
    echo ""
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
    echo "  Nas próximas sessões de terminal funcionará automaticamente."
else
    success "Pronto! Execute: multissh"
fi
