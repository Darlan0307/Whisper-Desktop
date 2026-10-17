"""
Transcreve áudios (WhatsApp .ogg e outros formatos) em português e junta
tudo em um único .txt, pronto para colar no chat.

COMO USAR
1. Coloque os áudios que você quer transcrever na pasta "audios", que fica
   ao lado deste script (se ela não existir, o script cria na 1ª execução).
2. Defina a ordem (escolha uma das formas):
   a) Pelo nome (padrão): renomeie com número na frente: 01-..., 02-..., 03-...
      Funciona também com audio-2 antes de audio-10.
   b) Com um arquivo "ordem.txt" dentro da pasta "audios": escreva um nome de
      arquivo por linha, na ordem desejada (a extensão é opcional; linhas que
      começam com # são ignoradas). Se existir, ele tem prioridade.
   c) Pela data de modificação dos arquivos:
          python transcrever_audios.py --ordem data
3. Rode:  python transcrever_audios.py   (ou dois cliques em transcrever.bat)

OPÇÕES
   --modelo small|medium|...   modelo do Whisper (padrão: small)
   --ordem auto|nome|data      como ordenar (padrão: auto = ordem.txt se
                               existir, senão pelo nome)
   --pasta CAMINHO             usar outra pasta de áudios

O resultado é salvo ao lado do script como transcricao_AAAA-MM-DD_HH-MM.txt.
Requer as dependências do requirements.txt.
"""

import argparse
import re
from datetime import datetime
from pathlib import Path

from faster_whisper import WhisperModel

EXTENSOES = {".ogg", ".opus", ".mp3", ".m4a", ".wav", ".aac", ".flac", ".mp4"}
PASTA_SCRIPT = Path(__file__).resolve().parent


def chave_natural(caminho: Path):
    """Ordenação natural: audio-2 vem antes de audio-10."""
    partes = re.split(r"(\d+)", caminho.name.lower())
    return [int(p) if p.isdigit() else p for p in partes]


def listar_audios(pasta: Path):
    return [f for f in pasta.iterdir() if f.is_file() and f.suffix.lower() in EXTENSOES]


def ordenar(arquivos, pasta: Path, modo: str):
    if modo == "data":
        return sorted(arquivos, key=lambda f: f.stat().st_mtime)

    por_nome = sorted(arquivos, key=chave_natural)
    arquivo_ordem = pasta / "ordem.txt"
    if modo == "nome" or not arquivo_ordem.exists():
        return por_nome

    mapa_nome = {f.name.lower(): f for f in arquivos}
    mapa_base = {f.stem.lower(): f for f in arquivos}
    ordenados, usados = [], set()

    for linha in arquivo_ordem.read_text(encoding="utf-8-sig").splitlines():
        item = linha.strip().strip('"')
        if not item or item.startswith("#"):
            continue
        achado = mapa_nome.get(item.lower()) or mapa_base.get(item.lower())
        if achado is None:
            print(f"  Aviso: '{item}' está no ordem.txt, mas não foi encontrado na pasta.")
        elif achado not in usados:
            ordenados.append(achado)
            usados.add(achado)

    restantes = [f for f in por_nome if f not in usados]
    if restantes:
        nomes = ", ".join(f.name for f in restantes)
        print(f"  Aviso: fora do ordem.txt (vão para o final, por nome): {nomes}")
        ordenados.extend(restantes)
    return ordenados


def main() -> None:
    ap = argparse.ArgumentParser(description="Transcreve áudios em português.")
    ap.add_argument("--pasta", default=str(PASTA_SCRIPT / "audios"))
    ap.add_argument("--modelo", default="small",
                    choices=["tiny", "base", "small", "medium", "large-v3"])
    ap.add_argument("--ordem", default="auto", choices=["auto", "nome", "data"])
    args = ap.parse_args()

    pasta = Path(args.pasta)
    if not pasta.exists():
        pasta.mkdir(parents=True)
        print(f"Criei a pasta: {pasta}")
        print("Coloque os áudios nela e rode o script de novo.")
        return

    arquivos = listar_audios(pasta)
    if not arquivos:
        print(f"Nenhum áudio encontrado em: {pasta}")
        print("Coloque os arquivos (.ogg, .mp3, .m4a, .wav...) nessa pasta e rode de novo.")
        return

    arquivos = ordenar(arquivos, pasta, args.ordem)
    print("\nOrdem de transcrição:")
    for i, f in enumerate(arquivos, start=1):
        print(f"  {i}. {f.name}")

    print(f"\nCarregando o modelo '{args.modelo}'...")
    modelo = WhisperModel(args.modelo, device="cpu", compute_type="int8")

    blocos = []
    for i, f in enumerate(arquivos, start=1):
        print(f"[{i}/{len(arquivos)}] Transcrevendo {f.name}...")
        try:
            segmentos, _info = modelo.transcribe(
                str(f),
                language="pt",
                vad_filter=True,  # ignora silêncios, reduz "alucinações"
                beam_size=5,
            )
            texto = " ".join(seg.text.strip() for seg in segmentos).strip()
        except Exception as erro:  # um áudio com problema não derruba os outros
            print(f"  Erro em {f.name}: {erro}")
            texto = f"[erro ao transcrever: {erro}]"
        blocos.append(f"Áudio {i}:\n{texto or '[sem fala detectada]'}")

    saida = PASTA_SCRIPT / f"transcricao_{datetime.now():%Y-%m-%d_%H-%M}.txt"
    saida.write_text("\n\n".join(blocos) + "\n", encoding="utf-8")
    print(f"\nPronto! Transcrição salva em: {saida}")


if __name__ == "__main__":
    main()
