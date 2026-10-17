"""
Lógica de transcrição (sem interface), reaproveitando as escolhas do
transcrever_audios.py: faster-whisper, modelo "small", CPU, int8, português,
vad_filter=True, beam_size=5, ordenação natural e saída "Áudio N:".

Todo o trabalho com o modelo roda em UMA thread de fundo (classe Transcritor):
ela carrega o modelo uma única vez e executa as tarefas em fila, uma de cada vez.
Assim as duas abas usam o mesmo modelo sem disputa e a janela nunca trava.
"""

import gc
import os
import queue
import re
import threading
import traceback
from pathlib import Path

# Nada de telemetria; a internet só é usada se o modelo não estiver no cache.
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

MODELO_PADRAO = "small"
MODELOS = ["tiny", "base", "small", "medium", "large-v3"]
EXTENSOES = {".ogg", ".opus", ".mp3", ".m4a", ".wav", ".aac", ".flac"}
TAXA_AMOSTRAGEM = 16000  # o Whisper trabalha em 16 kHz mono

PARAMETROS = dict(
    language="pt",
    vad_filter=True,  # ignora silêncios, reduz "alucinações"
    beam_size=5,
)


class Cancelado(Exception):
    """Levantada quando o usuário cancela uma transcrição em andamento."""


def chave_natural(caminho):
    """Ordenação natural: audio-2 vem antes de audio-10."""
    partes = re.split(r"(\d+)", Path(caminho).name.lower())
    return [int(p) if p.isdigit() else p for p in partes]


def ordenar_por_nome(arquivos):
    return sorted(arquivos, key=chave_natural)


def extensao_aceita(caminho) -> bool:
    return Path(caminho).suffix.lower() in EXTENSOES


def formatar_resultado(textos) -> str:
    """["oi", "tchau"] -> "Áudio 1:\\noi\\n\\nÁudio 2:\\ntchau"."""
    return "\n\n".join(f"Áudio {i}:\n{t}" for i, t in enumerate(textos, start=1))


def mensagem_erro_arquivo(caminho, erro: Exception) -> str:
    """Traduz erros comuns de leitura de arquivo para português."""
    caminho = Path(caminho)
    if not caminho.exists():
        return "arquivo não encontrado (foi movido ou apagado?)"
    if caminho.stat().st_size == 0:
        return "arquivo vazio"
    nome_erro = type(erro).__name__
    texto = str(erro)
    if isinstance(erro, PermissionError) or "Permission denied" in texto:
        return "sem permissão para ler o arquivo (está aberto em outro programa?)"
    # Erros do decodificador (biblioteca av/FFmpeg): arquivo que não é áudio válido.
    if type(erro).__module__.startswith("av") or "Invalid data" in texto or "End of file" in texto:
        return "arquivo inválido ou corrompido (não é um áudio que dê para ler)"
    return f"{nome_erro}: {texto}"


class Transcritor:
    """Dono do modelo Whisper. Tudo que usa o modelo passa pela fila da thread."""

    def __init__(self, ao_mudar_status=None):
        # ao_mudar_status(estado, mensagem) é chamado DA THREAD DE FUNDO;
        # a interface deve repassar para a thread da janela (ex.: com uma fila).
        self._ao_mudar_status = ao_mudar_status or (lambda estado, msg: None)
        self._fila = queue.Queue()
        self._modelo = None
        self.nome_modelo = None
        self._nome_desejado = MODELO_PADRAO
        self._trabalhando = False
        self.estado = "parado"  # parado | carregando | pronto | erro | liberado
        self._thread = threading.Thread(target=self._laco, name="transcritor", daemon=True)
        self._thread.start()

    # ---------- API pública (pode ser chamada da thread da janela) ----------

    def carregar(self, nome_modelo=None):
        """Agenda o carregamento (ou troca) do modelo."""
        if nome_modelo:
            self._nome_desejado = nome_modelo
        self._fila.put(("carregar", self._nome_desejado))

    def adiar(self, nome_modelo):
        """Define o modelo sem carregá-lo agora (carrega na primeira tarefa)."""
        self._nome_desejado = nome_modelo
        self._status("liberado", "Em espera: o modelo carrega quando for usado")

    def liberar(self):
        """Tira o modelo da memória (volta sozinho na próxima tarefa ou carregar())."""
        self._fila.put(("liberar", None))

    @property
    def ocioso(self) -> bool:
        return not self._trabalhando and self._fila.empty()

    def executar(self, funcao):
        """Agenda funcao(modelo) para rodar na thread de fundo, depois do
        carregamento. Se o modelo falhou ao carregar, funcao recebe None."""
        self._fila.put(("tarefa", funcao))

    def encerrar(self):
        self._fila.put(("sair", None))

    # ---------- thread de fundo ----------

    def _status(self, estado, mensagem):
        self.estado = estado
        try:
            self._ao_mudar_status(estado, mensagem)
        except Exception:
            pass

    def _laco(self):
        while True:
            tipo, dado = self._fila.get()
            if tipo == "sair":
                return
            self._trabalhando = True
            try:
                if tipo == "carregar":
                    self._carregar(dado)
                elif tipo == "liberar":
                    self._liberar()
                elif tipo == "tarefa":
                    if self._modelo is None and self.estado == "liberado":
                        self._carregar(self._nome_desejado)
                    dado(self._modelo)
            except Exception:
                # Quem agendou a tarefa deve tratar os próprios erros;
                # isto só impede que a thread morra.
                traceback.print_exc()
            finally:
                self._trabalhando = False

    def _liberar(self):
        if self._modelo is None:
            return
        self._modelo = None
        self.nome_modelo = None
        try:  # o detector de silêncio (VAD) fica num cache próprio: libera também
            from faster_whisper.vad import get_vad_model
            get_vad_model.cache_clear()
        except Exception:
            pass
        gc.collect()
        try:  # devolve ao Windows a RAM que ficou livre
            import ctypes
            import ctypes.wintypes as wt
            k32 = ctypes.WinDLL("kernel32")
            k32.GetCurrentProcess.restype = wt.HANDLE
            ps = ctypes.WinDLL("psapi")
            ps.EmptyWorkingSet.argtypes = [wt.HANDLE]
            ps.EmptyWorkingSet(k32.GetCurrentProcess())
        except Exception:
            pass
        self._status("liberado", "Em espera: modelo fora da memória (volta sozinho ao usar)")

    def _carregar(self, nome):
        if self._modelo is not None and nome == self.nome_modelo:
            self._status("pronto", f"Pronto (modelo {nome})")
            return
        from faster_whisper import WhisperModel  # import pesado: só aqui

        self._modelo = None
        self._status("carregando", f"Carregando modelo {nome}...")
        try:
            try:
                # 1ª tentativa: só do cache, sem tocar na internet.
                modelo = WhisperModel(nome, device="cpu", compute_type="int8",
                                      local_files_only=True)
            except Exception:
                self._status("carregando",
                             f"Baixando o modelo {nome} (só na primeira vez, pode demorar)...")
                modelo = WhisperModel(nome, device="cpu", compute_type="int8")
        except Exception as erro:
            self.nome_modelo = None
            self._status("erro", f"Não foi possível carregar o modelo {nome}: {erro}. "
                                 "Se for a primeira vez com este modelo, verifique a internet.")
            return
        self._modelo = modelo
        self.nome_modelo = nome
        self._status("pronto", f"Pronto (modelo {nome})")


def transcrever(modelo, audio, ao_progredir=None, cancelado=None) -> str:
    """Transcreve um caminho de arquivo (str/Path) ou um array numpy float32
    de 16 kHz mono. Deve ser chamada dentro de Transcritor.executar.

    ao_progredir(fração 0..1) é chamado a cada trecho reconhecido.
    cancelado() -> bool permite interromper entre trechos (levanta Cancelado).
    """
    if modelo is None:
        raise RuntimeError("o modelo não está carregado")
    if isinstance(audio, Path):
        audio = str(audio)
    segmentos, info = modelo.transcribe(audio, **PARAMETROS)
    duracao = info.duration or 0
    partes = []
    for seg in segmentos:  # o reconhecimento acontece enquanto iteramos
        if cancelado and cancelado():
            raise Cancelado()
        partes.append(seg.text.strip())
        if ao_progredir and duracao > 0:
            ao_progredir(min(seg.end / duracao, 1.0))
    if ao_progredir:
        ao_progredir(1.0)
    return " ".join(p for p in partes if p).strip()
