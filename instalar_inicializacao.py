"""
OPCIONAL: faz o app "Transcrição de Áudio" abrir sozinho quando o Windows
iniciar, já minimizado perto do relógio (atalho na pasta shell:startup).
Não precisa de administrador.

Uso:
    python instalar_inicializacao.py            ativa
    python instalar_inicializacao.py --remover  desativa
"""

import sys

from instalar_atalho import ICONE, NOME_ATALHO, criar_atalho, gerar_icone, pasta_especial


def main():
    local = pasta_especial("Startup") / NOME_ATALHO
    if "--remover" in sys.argv:
        if local.exists():
            local.unlink()
            print(f"Removido da inicialização: {local}")
        else:
            print("O app não estava na inicialização.")
        return
    if not ICONE.exists():
        gerar_icone()
    criar_atalho(local, "--minimizado")
    print(f"Pronto! O app vai abrir (perto do relógio) quando o Windows iniciar.\nAtalho: {local}")
    print("Para desfazer: python instalar_inicializacao.py --remover")


if __name__ == "__main__":
    main()
