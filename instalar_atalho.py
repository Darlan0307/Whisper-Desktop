"""
Cria atalhos do app "Transcrição de Áudio" na Área de Trabalho e no Menu
Iniciar (não precisa de administrador). Também gera o ícone icone.ico.

Uso:
    python instalar_atalho.py            cria/atualiza os atalhos
    python instalar_atalho.py --remover  apaga os atalhos
"""

import os
import struct
import subprocess
import sys
from pathlib import Path

PASTA_APP = Path(__file__).resolve().parent
sys.path.insert(0, str(PASTA_APP))

import icones  # noqa: E402

APP = PASTA_APP / "app.pyw"
ICONE = PASTA_APP / "icone.ico"
NOME_ATALHO = "Transcrição de Áudio.lnk"
DESCRICAO = "Transcrever voz e áudios do WhatsApp em texto (local)"

# Caminho completo: não depende do PATH (evita rodar um "powershell" falso).
POWERSHELL = str(Path(os.environ.get("SystemRoot", r"C:\Windows"))
                 / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


# ---------------------------------------------------------------- ícone

def gerar_icone(destino: Path = ICONE):
    """Grava um .ico com vários tamanhos (formato BMP 32 bits)."""
    tamanhos = [16, 24, 32, 48, 64, 128, 256]
    imagens = []
    for t in tamanhos:
        rgba = icones.icone_app(t)
        bgra = rgba[::-1, :, [2, 1, 0, 3]]  # BMP: de baixo para cima, ordem BGRA
        cabecalho = struct.pack("<IiiHHIIiiII", 40, t, t * 2, 1, 32, 0, 0, 0, 0, 0, 0)
        mascara = bytes(((t + 31) // 32) * 4 * t)  # máscara AND (transparência vem do alfa)
        imagens.append(cabecalho + bgra.tobytes() + mascara)
    saida = struct.pack("<HHH", 0, 1, len(tamanhos))
    deslocamento = 6 + 16 * len(tamanhos)
    for t, dados in zip(tamanhos, imagens):
        lado = 0 if t >= 256 else t
        saida += struct.pack("<BBBBHHII", lado, lado, 0, 0, 1, 32, len(dados), deslocamento)
        deslocamento += len(dados)
    destino.write_bytes(saida + b"".join(imagens))


# ---------------------------------------------------------------- atalhos

def pasta_especial(nome: str) -> Path:
    """Desktop, Programs (Menu Iniciar) ou Startup — respeitando o OneDrive."""
    r = subprocess.run([POWERSHELL, "-NoProfile", "-Command",
                        f"[Environment]::GetFolderPath('{nome}')"],
                       capture_output=True, text=True, check=True)
    return Path(r.stdout.strip())


def pythonw() -> Path:
    exe = Path(sys.executable).with_name("pythonw.exe")
    if not exe.exists():
        sys.exit(f"Não encontrei o pythonw.exe ao lado de {sys.executable}.")
    return exe


def criar_atalho(destino: Path, argumentos: str = ""):
    destino.parent.mkdir(parents=True, exist_ok=True)
    # Os caminhos vão por variáveis de ambiente para não ter problema com acentos/aspas.
    env = dict(os.environ,
               ATALHO=str(destino), ALVO=str(pythonw()), ARGS=f'"{APP}" {argumentos}'.strip(),
               PASTA=str(PASTA_APP), ICONE=f"{ICONE},0", DESC=DESCRICAO)
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:ATALHO);"
              "$s.TargetPath = $env:ALVO; $s.Arguments = $env:ARGS;"
              "$s.WorkingDirectory = $env:PASTA; $s.IconLocation = $env:ICONE;"
              "$s.Description = $env:DESC; $s.Save()")
    subprocess.run([POWERSHELL, "-NoProfile", "-Command", script], env=env, check=True)


def remover_atalho(destino: Path):
    if destino.exists():
        destino.unlink()
        print(f"  removido: {destino}")


def main():
    locais = [pasta_especial("Desktop") / NOME_ATALHO,
              pasta_especial("Programs") / NOME_ATALHO]
    if "--remover" in sys.argv:
        for local in locais:
            remover_atalho(local)
        print("Atalhos removidos.")
        return
    if not APP.exists():
        sys.exit(f"Não encontrei {APP}.")
    gerar_icone()
    print(f"Ícone criado: {ICONE}")
    for local in locais:
        criar_atalho(local)
        print(f"Atalho criado: {local}")
    print("\nPronto! Procure \"Transcrição de Áudio\" na Área de Trabalho ou no Menu Iniciar.")


if __name__ == "__main__":
    main()
