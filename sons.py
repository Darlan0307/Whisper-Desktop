"""
Sons curtos para avisar o fim de um ditado feito com o app em segundo plano.

Gerados com numpy (notas tipo "sino" suave: ataque rápido, decaimento
exponencial, volume baixo) e gravados uma vez em %APPDATA%\\TranscricaoAudio\\sons.
Tocam de forma assíncrona pelo winsound, sem dependências extras.
"""

import traceback
import wave
from pathlib import Path

import numpy as np

TAXA = 44100
VOLUME = 0.22  # pico bem abaixo do máximo: o volume do Windows manda no resto
VERSAO = 1     # mudar ao alterar os sons, para regravar os arquivos

# nome -> notas (frequência em Hz, início em segundos)
SONS = {
    "colado": [(784, 0.0), (1047, 0.09)],               # sol, dó: "feito"
    "copiado": [(784, 0.0), (1047, 0.09), (1319, 0.18)],  # sol, dó, mi: "pronto, cole você"
    "erro": [(659, 0.0), (494, 0.12)],                  # mi, si (descendo): "não deu"
}


def _nota(freq, duracao=0.45):
    t = np.arange(int(TAXA * duracao)) / TAXA
    envelope = np.minimum(t / 0.006, 1.0) * np.exp(-t / 0.11)
    onda = np.sin(2 * np.pi * freq * t) + 0.18 * np.sin(4 * np.pi * freq * t)
    return onda * envelope


def _gerar(notas):
    total = max(inicio for _, inicio in notas) + 0.45
    audio = np.zeros(int(TAXA * total))
    for freq, inicio in notas:
        nota = _nota(freq)
        i = int(TAXA * inicio)
        audio[i:i + nota.size] += nota
    audio *= VOLUME / np.max(np.abs(audio))
    return (audio * 32767).astype("<i2").tobytes()


class Sons:
    def __init__(self, pasta: Path):
        self._arquivos = {}
        try:
            import winsound
            self._winsound = winsound
        except ImportError:
            self._winsound = None
            return
        pasta.mkdir(parents=True, exist_ok=True)
        for nome, notas in SONS.items():
            arquivo = pasta / f"{nome}_v{VERSAO}.wav"
            try:
                if not arquivo.exists():
                    with wave.open(str(arquivo), "wb") as w:
                        w.setnchannels(1)
                        w.setsampwidth(2)
                        w.setframerate(TAXA)
                        w.writeframes(_gerar(notas))
                self._arquivos[nome] = str(arquivo)
            except OSError:
                traceback.print_exc()

    def tocar(self, nome):
        arquivo = self._arquivos.get(nome)
        if not arquivo or self._winsound is None:
            return
        ws = self._winsound
        try:
            ws.PlaySound(arquivo, ws.SND_FILENAME | ws.SND_ASYNC | ws.SND_NODEFAULT)
        except RuntimeError:
            traceback.print_exc()
