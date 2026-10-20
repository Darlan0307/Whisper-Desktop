"""
Lógica de transcrição (sem interface), reaproveitando as escolhas do
transcrever_audios.py: faster-whisper, modelo "small", português,
vad_filter=True, beam_size=5, ordenação natural e saída "Áudio N:".

Todo o trabalho com o modelo roda em UMA thread de fundo (classe Transcritor):
ela carrega o modelo uma única vez e executa as tarefas em fila, uma de cada vez.
Assim as duas abas usam o mesmo modelo sem disputa e a janela nunca trava.

Roda na GPU NVIDIA quando possível (precisa do requirements-gpu.txt) e volta
sozinho para o processador se a GPU não existir ou falhar. Enquanto trabalha,
o app fica com prioridade "abaixo do normal" para não travar os outros programas.
"""

import gc
import os
import queue
import re
import sys
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

# Metade dos processadores lógicos: a outra metade fica livre para os outros programas.
CPU_THREADS = max(2, min(8, (os.cpu_count() or 4) // 2))

PARAMETROS = dict(
    language="pt",
    vad_filter=True,  # ignora silêncios, reduz "alucinações"
    beam_size=5,
)

# Dica de contexto para o ditado: o Whisper passa a escrever esses termos do jeito
# certo ("commit" em vez de "camit", "Vercel" em vez de "Wercel"). Em forma de frase
# pontuada, porque o modelo também imita a pontuação do texto de exemplo.
VOCABULARIO_DEV = (
    "Conversa técnica sobre desenvolvimento de software: deploy, commit, push, pull request, "
    "branch, merge, rebase, Git, GitHub, build, pipeline, CI/CD, staging, produção, frontend, "
    "backend, API, endpoint, JSON, JWT, token, webhook, payload, React, Next.js, Node.js, "
    "TypeScript, JavaScript, Python, Docker, Supabase, PostgreSQL, Redis, query, cache, bug, "
    "debug, issue, refatorar, componente, hook, props, middleware, Tailwind, Vercel, npm, "
    "VS Code, Claude Code, prompt, LLM."
)


def _registrar_dlls_cuda():
    """As DLLs do CUDA instaladas pelo pip (pacotes nvidia-*-cu12) ficam em
    site-packages/nvidia/*/bin, fora dos lugares onde o Windows procura."""
    if sys.platform != "win32":
        return
    for base in {Path(p) for p in sys.path if p}:
        for pasta in (base / "nvidia").glob("*/bin"):
            try:
                os.add_dll_directory(str(pasta))
            except OSError:
                continue
            # O ctranslate2 carrega a cuBLAS só na 1ª transcrição, procurando pelo PATH.
            os.environ["PATH"] = str(pasta) + os.pathsep + os.environ.get("PATH", "")


_registrar_dlls_cuda()


def gpu_disponivel() -> bool:
    """True se há uma GPU NVIDIA utilizável (driver + cuBLAS instalada)."""
    try:
        import ctypes

        import ctranslate2
        if ctranslate2.get_cuda_device_count() < 1:
            return False
        ctypes.WinDLL("cublas64_12.dll")  # sem ela o modelo carrega, mas falha ao transcrever
        return True
    except Exception:
        return False


def _erro_de_gpu(erro: Exception) -> bool:
    texto = str(erro).lower()
    return any(p in texto for p in ("cuda", "cublas", "cudnn", "out of memory"))


def _prioridade_baixa(ativar: bool):
    """Prioridade "abaixo do normal" enquanto transcreve: se o PC estiver ocupado, o
    Windows atende os outros programas primeiro; se estiver livre, a velocidade é a mesma."""
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32")
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x4000 if ativar else 0x20)
    except Exception:
        pass


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
        self.dispositivo = None  # "cuda" | "cpu" do modelo carregado
        self._nome_desejado = MODELO_PADRAO
        self._dispositivo_desejado = "gpu"  # "gpu" (se houver) | "cpu"
        self._trabalhando = False
        self.estado = "parado"  # parado | carregando | pronto | erro | liberado
        self._thread = threading.Thread(target=self._laco, name="transcritor", daemon=True)
        self._thread.start()

    # ---------- API pública (pode ser chamada da thread da janela) ----------

    def carregar(self, nome_modelo=None, dispositivo=None):
        """Agenda o carregamento (ou troca) do modelo."""
        if nome_modelo:
            self._nome_desejado = nome_modelo
        if dispositivo:
            self._dispositivo_desejado = dispositivo
        self._fila.put(("carregar", self._nome_desejado))

    def adiar(self, nome_modelo, dispositivo=None):
        """Define o modelo sem carregá-lo agora (carrega na primeira tarefa)."""
        self._nome_desejado = nome_modelo
        if dispositivo:
            self._dispositivo_desejado = dispositivo
        self._status("liberado", "Em espera: o modelo carrega quando for usado")

    def liberar(self):
        """Tira o modelo da memória (volta sozinho na próxima tarefa ou carregar())."""
        self._fila.put(("liberar", None))

    @property
    def ocioso(self) -> bool:
        return not self._trabalhando and self._fila.empty()

    def executar(self, funcao):
        """Agenda funcao(transcrever) para rodar na thread de fundo, depois do
        carregamento. transcrever(audio, ao_progredir=None, cancelado=None,
        rapido=False, prompt=None) -> str levanta RuntimeError se o modelo não carregou."""
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
            _prioridade_baixa(True)
            try:
                if tipo == "carregar":
                    self._carregar(dado)
                elif tipo == "liberar":
                    self._liberar()
                elif tipo == "tarefa":
                    # "erro": tenta de novo (ex.: a internet voltou para baixar o modelo).
                    if self._modelo is None and self.estado in ("liberado", "erro"):
                        self._carregar(self._nome_desejado)
                    dado(self._transcrever)
            except Exception:
                # Quem agendou a tarefa deve tratar os próprios erros;
                # isto só impede que a thread morra.
                traceback.print_exc()
            finally:
                _prioridade_baixa(False)
                self._trabalhando = False

    def _transcrever(self, audio, ao_progredir=None, cancelado=None, rapido=False, prompt=None):
        if self._modelo is None:
            raise RuntimeError("o modelo não está carregado")
        opcoes = {}
        if rapido and self.dispositivo == "cpu":
            opcoes["beam_size"] = 1  # ~25% mais rápido no processador; na GPU não faz diferença
        if prompt:
            opcoes["initial_prompt"] = prompt
        try:
            return transcrever(self._modelo, audio, ao_progredir, cancelado, **opcoes)
        except Cancelado:
            raise
        except Exception as erro:
            if self.dispositivo != "cuda" or not _erro_de_gpu(erro):
                raise
            # Ex.: um jogo ocupou a memória da GPU. Refaz no processador; a GPU volta a
            # ser tentada quando o modelo for recarregado.
            traceback.print_exc()
            self._carregar(self.nome_modelo, forcar_cpu=True)
            if self._modelo is None:
                raise
            return transcrever(self._modelo, audio, ao_progredir, cancelado, **opcoes)

    def _liberar(self):
        if self._modelo is None:
            return
        self._modelo = None
        self.nome_modelo = None
        self.dispositivo = None
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

    def _carregar(self, nome, forcar_cpu=False):
        quer_gpu = self._dispositivo_desejado == "gpu" and not forcar_cpu
        if (self._modelo is not None and nome == self.nome_modelo
                and (self.dispositivo == "cuda") == quer_gpu):
            self._status("pronto", self._mensagem_pronto())
            return
        from faster_whisper import WhisperModel  # import pesado: só aqui
        from faster_whisper.utils import download_model

        self._modelo = None
        self.nome_modelo = None
        self.dispositivo = None
        gc.collect()  # solta o modelo anterior antes de ocupar memória com o novo
        self._status("carregando", f"Carregando modelo {nome}...")
        try:
            try:
                # 1ª tentativa: só do cache, sem tocar na internet.
                caminho = download_model(nome, local_files_only=True)
            except Exception:
                self._status("carregando",
                             f"Baixando o modelo {nome} (só na primeira vez, pode demorar)...")
                caminho = download_model(nome)
        except Exception as erro:
            self._status("erro", f"Não foi possível baixar o modelo {nome}: {erro}. "
                                 "Se for a primeira vez com este modelo, verifique a internet.")
            return

        modelo, aviso = None, ""
        if quer_gpu:
            if gpu_disponivel():
                try:
                    # int8_float16: metade da memória de vídeo do float16, mesma qualidade.
                    modelo = WhisperModel(caminho, device="cuda", compute_type="int8_float16")
                    _aquecer(modelo)  # já descobre aqui se a GPU funciona de verdade
                except Exception:
                    traceback.print_exc()
                    modelo = None
                    aviso = " · a GPU falhou, usando o processador"
            else:
                aviso = " · GPU não encontrada, usando o processador"
        elif forcar_cpu:
            aviso = " · a GPU falhou, usando o processador"
        if modelo is None:
            gc.collect()
            try:
                modelo = WhisperModel(caminho, device="cpu", compute_type="int8",
                                      cpu_threads=CPU_THREADS)
            except Exception as erro:
                self._status("erro", f"Não foi possível carregar o modelo {nome}: {erro}")
                return
        self._modelo = modelo
        self.nome_modelo = nome
        self.dispositivo = "cuda" if modelo.model.device == "cuda" else "cpu"
        self._status("pronto", self._mensagem_pronto() + aviso)

    def _mensagem_pronto(self):
        onde = "GPU" if self.dispositivo == "cuda" else "processador"
        return f"Pronto (modelo {self.nome_modelo}, {onde})"


def _aquecer(modelo):
    import numpy as np
    segmentos, _ = modelo.transcribe(np.zeros(TAXA_AMOSTRAGEM, dtype=np.float32),
                                     language="pt", beam_size=1)
    list(segmentos)


def transcrever(modelo, audio, ao_progredir=None, cancelado=None, **opcoes) -> str:
    """Transcreve um caminho de arquivo (str/Path) ou um array numpy float32
    de 16 kHz mono. Deve ser chamada dentro de Transcritor.executar.

    ao_progredir(fração 0..1) é chamado a cada trecho reconhecido.
    cancelado() -> bool permite interromper entre trechos (levanta Cancelado).
    opcoes substituem as de PARAMETROS (ex.: beam_size, initial_prompt).
    """
    if modelo is None:
        raise RuntimeError("o modelo não está carregado")
    if isinstance(audio, Path):
        audio = str(audio)
    if cancelado and cancelado():
        raise Cancelado()
    segmentos, info = modelo.transcribe(audio, **{**PARAMETROS, **opcoes})
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
